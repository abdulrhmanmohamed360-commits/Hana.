"""Knowledge Quality: explainable per-dimension assessment of learning assets and lessons (snapshots are append-only)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.engines.quality_rules import RUBRIC_VERSION, Assessment, ClaimFact, EvidenceFact, QualityInput, assess
from app.models import (CurriculumNode, EvidenceRecord, ExtractedClaim, GraphEdge, LearningAsset, QualityAssessment,
                        Source)
from app.models.enums import ClaimStatus, SourcePolicy
from app.services import audit, coverage

_VERDICT_FALLBACK = {ClaimStatus.VERIFIED: "MATH_VERIFIED", ClaimStatus.REJECTED: "REJECTED", ClaimStatus.CONFLICTED: "CONFLICTED"}


def _age_days(a: LearningAsset) -> float | None:
    ref = coverage._aware(a.reviewed_at) or coverage._aware(a.updated_at) or coverage._aware(a.created_at)
    return (datetime.now(timezone.utc) - ref).total_seconds() / 86400 if ref else None


def build_input(db: Session, a: LearningAsset) -> QualityInput:
    claims = list(db.scalars(select(ExtractedClaim).where(ExtractedClaim.learning_asset_id == a.id)))
    ev_by_claim: dict[uuid.UUID, list[EvidenceRecord]] = {}
    if claims:
        for ev in db.scalars(select(EvidenceRecord).where(EvidenceRecord.claim_id.in_([c.id for c in claims]))):
            ev_by_claim.setdefault(ev.claim_id, []).append(ev)
    src_ids = {e.source_id for evs in ev_by_claim.values() for e in evs if e.source_id}
    sources = {s.id: s for s in db.scalars(select(Source).where(Source.id.in_(src_ids)))} if src_ids else {}
    facts = []
    for c in claims:
        verdict = (c.meta or {}).get("verification_status") or _VERDICT_FALLBACK.get(c.status, "UNVERIFIED")
        evs = [EvidenceFact(authority=e.authority_score,
                            usable=(e.source_id is None or (sources.get(e.source_id) and sources[e.source_id].policy == SourcePolicy.ALLOWED)),
                            group=e.independence_group or str(e.raw_document_id), stance=e.stance.value) for e in ev_by_claim.get(c.id, [])]
        facts.append(ClaimFact(verdict, evs))
    hosts = {urlparse(str(s.get("url") or s.get("source_url") or "")).hostname for s in (a.sources or []) if isinstance(s, dict)}
    hosts.discard(None)
    registered = list(db.scalars(select(Source).where(Source.host.in_(hosts), Source.deleted_at.is_(None),
                                                      Source.policy == SourcePolicy.ALLOWED))) if hosts else []
    contradictions = db.scalars(select(GraphEdge.id).where(
        GraphEdge.relation_type == "CONTRADICTS", GraphEdge.deleted_at.is_(None),
        or_(GraphEdge.from_id == a.id, GraphEdge.to_id == a.id))).all()
    completeness, fresh = None, 730
    if a.curriculum_node_id:
        node = db.get(CurriculumNode, a.curriculum_node_id)
        if node is not None:
            profile = coverage.load_profile(db, a.subject)
            fresh = profile.freshness_days or 730
            completeness = coverage.lesson_state(db, node, a.subject)["score"]
    return QualityInput(review_status=a.status, claims=facts, source_authorities=[s.authority_score for s in registered],
                        cited_sources=len([s for s in (a.sources or []) if s]), completeness=completeness,
                        contradiction_edges=len(contradictions), age_days=_age_days(a), freshness_days=fresh)


def to_dict(a: Assessment) -> dict:
    return {"rubric_version": RUBRIC_VERSION, "support_score": a.support_score, "correctness": a.correctness,
            "correctness_basis": a.correctness_basis, "needs_human_review": a.needs_human_review, "flags": a.flags,
            "dimensions": {k: {"score": v.score, "reason": v.reason} for k, v in a.dimensions.items()}, "note": a.note}


def _snapshot(db: Session, etype: str, eid: uuid.UUID, a: Assessment) -> QualityAssessment:
    row = QualityAssessment(entity_type=etype, entity_id=eid, rubric_version=RUBRIC_VERSION, support_score=a.support_score,
                            correctness=a.correctness, flags=a.flags, needs_human_review=a.needs_human_review,
                            dimensions={k: {"score": v.score, "reason": v.reason} for k, v in a.dimensions.items()})
    db.add(row)
    return row


def assess_asset(db: Session, asset_id: uuid.UUID, *, persist: bool = True, actor: str = "system") -> dict:
    a = db.get(LearningAsset, asset_id)
    if a is None:
        raise NotFoundError("learning asset not found")
    result = assess(build_input(db, a))
    if persist:
        _snapshot(db, "learning_asset", a.id, result)
        audit.record(db, actor=actor, action="quality.assess", entity_type="learning_asset", entity_id=a.id,
                     details={"support_score": result.support_score, "correctness": result.correctness})
    return {"asset_id": str(a.id), "status": a.status, **to_dict(result)}


def assess_lesson(db: Session, node_id: uuid.UUID, *, persist: bool = True, actor: str = "system") -> dict:
    node = db.get(CurriculumNode, node_id)
    if node is None or node.deleted_at is not None:
        raise NotFoundError("curriculum node not found")
    assets = list(db.scalars(select(LearningAsset).where(LearningAsset.curriculum_node_id == node.id,
                                                         LearningAsset.status.notin_(["REJECTED", "SUPERSEDED"]))))
    per = [assess_asset(db, x.id, persist=persist, actor=actor) for x in assets]
    order = ["REJECTED", "CONFLICTED", "UNKNOWN", "DETERMINISTICALLY_VERIFIED"]
    worst = min((p["correctness"] for p in per), key=order.index) if per else "UNKNOWN"
    flags = sorted({f for p in per for f in p["flags"]})
    return {"node_id": str(node.id), "lesson": node.name, "assets_assessed": len(per),
            "support_score": round(sum(p["support_score"] for p in per) / len(per), 3) if per else 0.0,
            "correctness": worst, "flags": flags, "needs_human_review": any(p["needs_human_review"] for p in per) or not per,
            "assets": per}


def history(db: Session, entity_type: str, entity_id: uuid.UUID, limit: int = 20) -> list[dict]:
    rows = db.scalars(select(QualityAssessment).where(QualityAssessment.entity_type == entity_type,
                                                      QualityAssessment.entity_id == entity_id)
                      .order_by(QualityAssessment.assessed_at.desc()).limit(limit))
    return [{"assessed_at": r.assessed_at, "support_score": r.support_score, "correctness": r.correctness,
             "flags": r.flags, "rubric_version": r.rubric_version} for r in rows]
