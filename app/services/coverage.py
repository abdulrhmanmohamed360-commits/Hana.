"""Knowledge Coverage Engine: compares curriculum structure with stored content using configurable completeness profiles."""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engines.coverage_rules import (PROFILES, AssetFacts, LessonCoverage, Profile, Requirement, evaluate_lesson,
                                        pick_profile_key, summarize)
from app.engines.curriculum_rules import match_name, term_number
from app.models import (CoverageProfile, Curriculum, CurriculumNode, ExtractedClaim, GraphEdge, LearningAsset)
from app.models.enums import ClaimStatus
from app.services import curriculum as cur_svc

_OPEN_CLAIM = (ClaimStatus.RAW, ClaimStatus.EXTRACTED, ClaimStatus.PENDING_VERIFICATION, ClaimStatus.CONFLICTED)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def profile_from_row(row: CoverageProfile) -> Profile:
    reqs = tuple(Requirement(asset_type=r["asset_type"], min_count=int(r.get("min_count", 1)),
                             min_chars=int(r.get("min_chars", 0)), require_sources=bool(r.get("require_sources", False)),
                             require_answers=bool(r.get("require_answers", False))) for r in row.requirements)
    return Profile(row.key, reqs, row.freshness_days)


def load_profile(db: Session, subject: str) -> Profile:
    """Custom DB profile whose subject_pattern matches wins; then a DB override of the built-in key; then the built-in."""
    rows = list(db.scalars(select(CoverageProfile)))
    for r in rows:
        if r.subject_pattern and match_name(r.subject_pattern, subject) and r.requirements:
            return profile_from_row(r)
    key = pick_profile_key(subject)
    for r in rows:
        if r.key == key and r.requirements:
            return profile_from_row(r)
    return PROFILES[key]


def _facts(a: LearningAsset, claims: dict[uuid.UUID, tuple[int, int]], now: datetime) -> AssetFacts:
    structured = ((a.meta or {}).get("structured") or {}).get("items") or []
    answered = sum(1 for it in structured if (it.get("data") or {}).get("answer") or (it.get("data") or {}).get("correct_label"))
    items = len(structured)
    if not items and (a.meta or {}).get("answers_included") is True:   # reviewer-confirmed plain-text answers
        items = answered = 1
    ref = _aware(a.reviewed_at) or _aware(a.updated_at) or _aware(a.created_at)
    unv, rej = claims.get(a.id, (0, 0))
    return AssetFacts(id=str(a.id), asset_type=a.asset_type, status=a.status, chars=len((a.content or "").strip()),
                      has_sources=bool(a.sources), items=items, items_with_answers=answered,
                      age_days=((now - ref).total_seconds() / 86400) if ref else None, unverified_claims=unv, rejected_claims=rej)


def asset_facts_for(db: Session, node_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[AssetFacts]]:
    now = datetime.now(timezone.utc)
    out: dict[uuid.UUID, list[AssetFacts]] = defaultdict(list)
    for i in range(0, len(node_ids), 400):
        chunk = node_ids[i:i + 400]
        assets = list(db.scalars(select(LearningAsset).where(LearningAsset.curriculum_node_id.in_(chunk))))
        counts: dict[uuid.UUID, list[int]] = defaultdict(lambda: [0, 0])
        if assets:
            for aid, status in db.execute(select(ExtractedClaim.learning_asset_id, ExtractedClaim.status).where(
                    ExtractedClaim.learning_asset_id.in_([a.id for a in assets]))):
                if status in _OPEN_CLAIM:
                    counts[aid][0] += 1
                elif status == ClaimStatus.REJECTED:
                    counts[aid][1] += 1
        for a in assets:
            out[a.curriculum_node_id].append(_facts(a, {k: tuple(v) for k, v in counts.items()}, now))
    return out


def lessons_in_scope(db: Session, *, country: str | None = None, grade: str | None = None, term: int | None = None,
                     subject: str | None = None, unit: str | None = None, curriculum_id: uuid.UUID | None = None,
                     education_system: str | None = None) -> list[dict]:
    if curriculum_id:
        curricula = [cur_svc.get_curriculum(db, curriculum_id)]
    else:
        curricula = [c for c in db.scalars(select(Curriculum).where(Curriculum.status == "ACTIVE"))
                     if (not country or match_name(c.country, country))
                     and (not education_system or match_name(c.education_system, education_system))]
    out = []
    for cur in curricula:
        nodes = cur_svc.load_nodes(db, cur.id)
        for n in nodes.values():
            if n.level != "LESSON":
                continue
            p = cur_svc.path_of(nodes, n)
            if grade and not ("GRADE" in p and match_name(p["GRADE"].name, grade)):
                continue
            if subject and not ("SUBJECT" in p and match_name(p["SUBJECT"].name, subject)):
                continue
            if unit and not ("UNIT" in p and match_name(p["UNIT"].name, unit)):
                continue
            if term is not None and "TERM" in p and term_number(p["TERM"].name) not in (None, term):
                continue
            out.append({"curriculum": cur, "node": n, "path": p})
    out.sort(key=lambda r: (r["curriculum"].name, r["path"].get("GRADE").name if "GRADE" in r["path"] else "",
                            r["path"].get("SUBJECT").name if "SUBJECT" in r["path"] else "", r["node"].position))
    return out


def _findings(db: Session, lesson_id: uuid.UUID, facts: list[AssetFacts]) -> dict:
    """Fine-grained gaps the lesson-level status does not show."""
    kids = list(db.scalars(select(CurriculumNode).where(CurriculumNode.parent_id == lesson_id,
                                                        CurriculumNode.level.in_(["TOPIC", "CONCEPT"]),
                                                        CurriculumNode.deleted_at.is_(None))))
    explained = set(db.scalars(select(GraphEdge.to_id).where(
        GraphEdge.relation_type == "EXPLAINS", GraphEdge.to_id.in_([k.id for k in kids] or [None]),
        GraphEdge.deleted_at.is_(None)))) if kids else set()
    explained |= set(db.scalars(select(LearningAsset.curriculum_node_id).where(
        LearningAsset.curriculum_node_id.in_([k.id for k in kids] or [None]),
        LearningAsset.asset_type.in_(["explanation", "definition", "glossary"]),
        LearningAsset.status == "APPROVED"))) if kids else set()
    formulas = [f for f in facts if f.asset_type == "formula" and f.status != "REJECTED"]
    with_example = set(db.scalars(select(GraphEdge.from_id).where(
        GraphEdge.relation_type == "HAS_EXAMPLE", GraphEdge.deleted_at.is_(None),
        GraphEdge.from_id.in_([uuid.UUID(f.id) for f in formulas] or [None])))) if formulas else set()
    return {
        "concepts_without_explanation": [k.name for k in kids if k.id not in explained],
        "formulas_without_examples": [f.id for f in formulas if uuid.UUID(f.id) not in with_example],
        "exercises_without_answers": [f.id for f in facts if f.asset_type in ("exercise", "quiz") and f.status != "REJECTED"
                                      and (f.items == 0 or f.items_with_answers < f.items)],
        "assets_needing_sources": [f.id for f in facts if not f.has_sources and f.status != "REJECTED"
                                   and f.asset_type in ("explanation", "definition", "formula")],
    }


def lesson_state(db: Session, node: CurriculumNode, subject_name: str, facts: list[AssetFacts] | None = None) -> dict:
    profile = load_profile(db, subject_name)
    facts = facts if facts is not None else asset_facts_for(db, [node.id]).get(node.id, [])
    cov = evaluate_lesson(profile, facts)
    return {"profile": profile.key, "status": cov.status, "score": cov.score, "met": cov.met, "pending_review": cov.pending,
            "unmet": cov.unmet, "outdated_assets": cov.outdated_assets, "unverified_claims": cov.unverified_claims,
            "pending_reviews": cov.pending_reviews, "reasons": cov.reasons,
            "findings": _findings(db, node.id, facts)}


def coverage_report(db: Session, *, details: bool = True, **filters) -> dict:
    lessons = lessons_in_scope(db, **filters)
    facts = asset_facts_for(db, [r["node"].id for r in lessons])
    rows, subjects = [], set()
    for r in lessons:
        node, p = r["node"], r["path"]
        subj = p["SUBJECT"].name if "SUBJECT" in p else ""
        if "SUBJECT" in p:
            subjects.add(p["SUBJECT"].id)
        st = lesson_state(db, node, subj, facts.get(node.id, []))
        rows.append({"lesson_id": str(node.id), "lesson": node.name, "curriculum": r["curriculum"].name,
                     "curriculum_version": r["curriculum"].version,
                     "grade": p["GRADE"].name if "GRADE" in p else None, "term": p["TERM"].name if "TERM" in p else None,
                     "subject": subj or None, "unit": p["UNIT"].name if "UNIT" in p else None, **st})
    statuses, scores = [x["status"] for x in rows], [x["score"] for x in rows]
    totals = summarize(statuses, scores)
    totals.update(total_subjects=len(subjects), unverified_claims=sum(x["unverified_claims"] for x in rows),
                  pending_reviews=sum(x["pending_reviews"] for x in rows),
                  outdated_knowledge=sum(len(x["outdated_assets"]) for x in rows),
                  concepts_without_explanation=sum(len(x["findings"]["concepts_without_explanation"]) for x in rows),
                  formulas_without_examples=sum(len(x["findings"]["formulas_without_examples"]) for x in rows),
                  exercises_without_answers=sum(len(x["findings"]["exercises_without_answers"]) for x in rows),
                  assets_needing_sources=sum(len(x["findings"]["assets_needing_sources"]) for x in rows))

    def group(key):
        buckets: dict = defaultdict(list)
        for x in rows:
            buckets[x[key]].append(x)
        return [{key: k, **summarize([y["status"] for y in v], [y["score"] for y in v])} for k, v in sorted(buckets.items(), key=lambda kv: str(kv[0]))]
    report = {"scope": {k: (str(v) if v is not None else None) for k, v in filters.items()}, "totals": totals,
              "by_subject": group("subject"), "by_grade": group("grade"), "by_unit": group("unit")}
    if details:
        report["lessons"] = rows
    return report
