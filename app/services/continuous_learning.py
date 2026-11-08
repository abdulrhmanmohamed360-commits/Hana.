"""Continuous learning: gap detection, bounded research planning, scan runs (history/retry/cancel), schedules, revisions.

Nothing here approves anything. The loop only (1) records what is missing/stale/unverified as KnowledgeGap rows,
(2) queues bounded research, (3) asks the preparation workflow for DRAFT content, and (4) sends it to human review.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.models import (EvidenceRecord, ExtractedClaim, KnowledgeGap, LearningAsset, RawDocument, ScanRun,
                        ScanSchedule, Source, SourceSnapshot, CurriculumNode)
from app.models.enums import ClaimStatus, TaskStatus
from app.models.ops import ResearchTask
from app.schemas.prepare import PrepareRequest
from app.services import audit, coverage, graph, quality
from app.services import curriculum as cur_svc

OPEN_STATES = ("OPEN", "IN_PROGRESS", "REVIEW")
RESEARCH_GAP_TYPES = {"MISSING_LESSON", "INCOMPLETE_CONTENT", "NEEDS_SOURCES", "OUTDATED_CONTENT", "SOURCE_CHANGED",
                      "CONCEPT_WITHOUT_EXPLANATION"}
_REQ_TO_CONTENT = {"explanation": "explanation", "definition": "definitions", "formula": "formulas", "example": "solved_examples",
                   "exercise": "exercises", "quiz": "questions", "summary": "summary"}
ACTIVE_SCAN = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING)


class ScanCancelled(Exception):
    pass


# ------------------------------------------------------------------ gaps
def _fingerprint(gap_type: str, node_id, asset_id, key: str = "") -> str:
    return hashlib.sha256(f"{gap_type}|{node_id}|{asset_id}|{key}".encode()).hexdigest()[:40]


def open_gap(db: Session, *, gap_type: str, description: str, actor: str, node_id=None, asset_id=None, key: str = "",
             priority: int = 5, detail: dict | None = None, scan_id=None) -> tuple[KnowledgeGap, bool]:
    fp = _fingerprint(gap_type, node_id, asset_id, key)
    existing = db.scalar(select(KnowledgeGap).where(KnowledgeGap.fingerprint == fp, KnowledgeGap.status.in_(OPEN_STATES)))
    if existing is not None:
        existing.detail = {**(existing.detail or {}), **(detail or {})}      # refresh facts, never duplicate
        return existing, False
    gap = KnowledgeGap(gap_type=gap_type, description=description, node_id=node_id, asset_id=asset_id, priority=priority,
                       detail=detail or {}, fingerprint=fp, detected_by=actor, scan_run_id=scan_id)
    db.add(gap)
    db.flush()
    audit.record(db, actor=actor, action="gap.open", entity_type="knowledge_gap", entity_id=gap.id,
                 details={"type": gap_type, "node_id": str(node_id) if node_id else None, "priority": priority})
    return gap, True


def detect_gaps(db: Session, scope: dict, *, actor: str, scan_id=None, cap: int = 200) -> dict:
    report = coverage.coverage_report(db, details=True, **{k: v for k, v in scope.items() if k in
                                      ("country", "grade", "term", "subject", "unit", "curriculum_id", "education_system")})
    created = refreshed = 0

    def add(**kw):
        nonlocal created, refreshed
        if created >= cap:
            return
        _, new = open_gap(db, actor=actor, scan_id=scan_id, **kw)
        created += new
        refreshed += (not new)

    for row in report.get("lessons", []):
        nid, name, st = uuid.UUID(row["lesson_id"]), row["lesson"], row["status"]
        if st == "MISSING":
            add(gap_type="MISSING_LESSON", node_id=nid, priority=2, description=f"No content exists for lesson '{name}'",
                detail={"unmet": row["unmet"]})
        elif row["unmet"]:
            add(gap_type="INCOMPLETE_CONTENT", node_id=nid, priority=3, key=",".join(sorted(u["asset_type"] for u in row["unmet"])),
                description=f"Lesson '{name}' is incomplete: " + "; ".join(row["reasons"][:3]), detail={"unmet": row["unmet"]})
        if row["pending_reviews"]:
            add(gap_type="NEEDS_REVIEW", node_id=nid, priority=4, description=f"Lesson '{name}' has drafts awaiting human review",
                detail={"pending": row["pending_review"], "count": row["pending_reviews"]})
        if row["outdated_assets"]:
            add(gap_type="OUTDATED_CONTENT", node_id=nid, priority=4, description=f"Lesson '{name}' has outdated approved content",
                detail={"asset_ids": row["outdated_assets"]})
        if row["unverified_claims"]:
            add(gap_type="UNVERIFIED_CLAIMS", node_id=nid, priority=5, description=f"Lesson '{name}' has {row['unverified_claims']} unverified claim(s)",
                detail={"count": row["unverified_claims"]})
        f = row["findings"]
        for concept in f["concepts_without_explanation"]:
            add(gap_type="CONCEPT_WITHOUT_EXPLANATION", node_id=nid, key=concept, priority=3,
                description=f"Concept '{concept}' in '{name}' has no approved explanation", detail={"concept": concept})
        if f["formulas_without_examples"]:
            add(gap_type="FORMULA_WITHOUT_EXAMPLE", node_id=nid, priority=4, description=f"Formulas in '{name}' have no examples",
                detail={"asset_ids": f["formulas_without_examples"]})
        if f["exercises_without_answers"]:
            add(gap_type="EXERCISE_WITHOUT_ANSWER", node_id=nid, priority=3, description=f"Exercises in '{name}' lack answers",
                detail={"asset_ids": f["exercises_without_answers"]})
        if f["assets_needing_sources"]:
            add(gap_type="NEEDS_SOURCES", node_id=nid, priority=4, description=f"Content in '{name}' cites no sources",
                detail={"asset_ids": f["assets_needing_sources"]})
        for miss in graph.missing_prerequisites(db, nid):
            add(gap_type="MISSING_PREREQUISITE", node_id=uuid.UUID(miss["node_id"]), key=str(nid), priority=3,
                description=f"Prerequisite '{miss['name']}' of '{name}' has no approved explanation",
                detail={"needed_by": str(nid), "needed_by_name": name})
    return {"lessons_scanned": len(report.get("lessons", [])), "gaps_opened": created, "gaps_refreshed": refreshed,
            "coverage_percentage": report["totals"]["coverage_percentage"]}


def detect_source_changes(db: Session, *, actor: str, scan_id=None) -> int:
    """A URL fetched more than once with different content hashes: evidence taken from an older version is stale."""
    multi = db.execute(select(SourceSnapshot.source_id, SourceSnapshot.url).group_by(
        SourceSnapshot.source_id, SourceSnapshot.url).having(func.count(func.distinct(SourceSnapshot.content_hash)) > 1)).all()
    opened = 0
    for source_id, url in multi:
        snaps = list(db.scalars(select(SourceSnapshot).where(SourceSnapshot.source_id == source_id, SourceSnapshot.url == url)
                                .order_by(SourceSnapshot.fetched_at.desc())))
        old_ids = [s.id for s in snaps[1:] if s.content_hash != snaps[0].content_hash]
        asset_ids = set(db.scalars(select(ExtractedClaim.learning_asset_id).join(EvidenceRecord, EvidenceRecord.claim_id == ExtractedClaim.id)
                                   .join(RawDocument, RawDocument.id == EvidenceRecord.raw_document_id)
                                   .where(RawDocument.snapshot_id.in_(old_ids), ExtractedClaim.learning_asset_id.is_not(None))))
        for aid in asset_ids:
            _, new = open_gap(db, gap_type="SOURCE_CHANGED", asset_id=aid, key=url, priority=4, actor=actor, scan_id=scan_id,
                              description=f"A source used as evidence changed after it was used: {url}",
                              detail={"url": url, "latest_snapshot": str(snaps[0].id)})
            opened += new
    return opened


def detect_conflicts(db: Session, *, actor: str, scan_id=None) -> int:
    opened = 0
    for c in db.scalars(select(ExtractedClaim).where(ExtractedClaim.status == ClaimStatus.CONFLICTED,
                                                     ExtractedClaim.learning_asset_id.is_not(None)).limit(500)):
        _, new = open_gap(db, gap_type="NEW_CONFLICT", asset_id=c.learning_asset_id, key=str(c.id), priority=2,
                          actor=actor, scan_id=scan_id, description="A claim conflicts with a retrieved source passage",
                          detail={"claim_id": str(c.id), "claim": c.claim_text[:300], "note": (c.meta or {}).get("detail")})
        opened += new
    return opened


# ------------------------------------------------------------------ bounded research planning
def research_budget_left(db: Session) -> int:
    s = get_settings()
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    used = db.scalar(select(func.count()).select_from(ResearchTask).where(
        ResearchTask.idempotency_key.like("gap:%"), ResearchTask.created_at >= start)) or 0
    return max(0, s.learning_max_research_per_day - used)


def plan_research(db: Session, *, actor: str, log: list[str]) -> int:
    s = get_settings()
    if s.tavily_api_key is None or not s.tavily_api_key.get_secret_value().strip():
        log.append("external research skipped: no search provider configured (TAVILY_API_KEY); gaps stay OPEN")
        return 0
    allowed = min(s.learning_max_research_per_scan, research_budget_left(db))
    if allowed <= 0:
        log.append("external research skipped: daily budget exhausted")
        return 0
    queued = 0
    gaps = db.scalars(select(KnowledgeGap).where(KnowledgeGap.status == "OPEN", KnowledgeGap.research_task_id.is_(None),
                                                 KnowledgeGap.gap_type.in_(RESEARCH_GAP_TYPES))
                      .order_by(KnowledgeGap.priority, KnowledgeGap.created_at).limit(allowed))
    for gap in gaps:
        task = ResearchTask(goal=f"[gap:{gap.gap_type}] {gap.description}"[:2000], created_by=actor, status=TaskStatus.PENDING,
                            idempotency_key=f"gap:{gap.fingerprint}", priority=gap.priority,
                            meta={"origin": "continuous_learning", "gap_id": str(gap.id), "publication_requires_review": True})
        db.add(task)
        db.flush()
        gap.research_task_id, gap.status = task.id, "IN_PROGRESS"
        queued += 1
    return queued


# ------------------------------------------------------------------ gap -> draft content (via the preparation workflow)
def prepare_for_gap(db: Session, gap_id: uuid.UUID, *, actor: str):
    from app.services.preparation_jobs import create_or_get_job
    gap = db.get(KnowledgeGap, gap_id)
    if gap is None:
        raise NotFoundError("gap not found")
    if gap.status not in ("OPEN", "IN_PROGRESS"):
        raise ConflictError(f"gap is {gap.status}; only OPEN/IN_PROGRESS gaps can be prepared")
    if gap.node_id is None:
        raise InvalidInputError("this gap is not attached to a lesson, so there is nothing to prepare")
    node = db.get(CurriculumNode, gap.node_id)
    if node is None or node.level != "LESSON":
        raise InvalidInputError("preparation needs a LESSON node")
    cur = cur_svc.get_curriculum(db, node.curriculum_id)
    p = cur_svc.path_of(cur_svc.load_nodes(db, cur.id), node)
    types = [_REQ_TO_CONTENT[u["asset_type"]] for u in (gap.detail or {}).get("unmet", []) if u["asset_type"] in _REQ_TO_CONTENT]
    types = list(dict.fromkeys(types)) or ["explanation", "definitions", "solved_examples", "exercises", "questions", "answers", "summary", "sources"]
    req = PrepareRequest(country=cur.country, curriculum=cur.name, grade=p["GRADE"].name, subject=p["SUBJECT"].name,
                         semester=cur_svc.term_number(p["TERM"].name) if "TERM" in p else None,
                         unit=p["UNIT"].name if "UNIT" in p else None, lesson=node.name, requested_content=types,
                         language=cur.language, mode="async", curriculum_id=cur.id)
    job, dup = create_or_get_job(db, req, actor)
    gap.preparation_job_id, gap.status = job.id, "IN_PROGRESS"
    audit.record(db, actor=actor, action="gap.prepare", entity_type="knowledge_gap", entity_id=gap.id,
                 details={"job_id": str(job.id), "types": types, "deduplicated": dup})
    db.commit()
    return job, dup


def create_revision(db: Session, asset_id: uuid.UUID, *, actor: str, content: str, reason: str,
                    gap_id: uuid.UUID | None = None) -> LearningAsset:
    """A new DRAFT version of an asset. The approved original stays authoritative until an admin approves the revision."""
    old = db.get(LearningAsset, asset_id)
    if old is None:
        raise NotFoundError("learning asset not found")
    if old.status not in ("APPROVED", "REVIEW", "DRAFT"):
        raise ConflictError(f"cannot revise an asset in status {old.status}")
    latest = old.version
    new = LearningAsset(education_system=old.education_system, stage=old.stage, grade=old.grade, subject=old.subject,
                        unit=old.unit, lesson=old.lesson, asset_type=old.asset_type, title=old.title, content=content,
                        language=old.language, status="DRAFT", version=latest + 1, quality_score=0.0, sources=list(old.sources or []),
                        learning_objectives=list(old.learning_objectives or []), tags=[*(old.tags or []), "revision", "needs-review"],
                        created_by=actor, curriculum_node_id=old.curriculum_node_id,
                        meta={**(old.meta or {}), "supersedes_asset_id": str(old.id), "revision_reason": reason,
                              "structured": None, "review_required": True})
    db.add(new)
    db.flush()
    if gap_id:
        gap = db.get(KnowledgeGap, gap_id)
        if gap is not None and gap.status in OPEN_STATES:
            gap.status, gap.detail = "REVIEW", {**(gap.detail or {}), "revision_asset_id": str(new.id)}
    audit.record(db, actor=actor, action="asset.revision_created", entity_type="learning_asset", entity_id=new.id,
                 details={"supersedes": str(old.id), "reason": reason, "version": new.version})
    return new


# ------------------------------------------------------------------ scan runs
def create_scan(db: Session, scope: dict, *, actor: str, kind: str = "MANUAL", schedule_id=None, retry_of=None) -> ScanRun:
    run = ScanRun(kind=kind, scope=scope, created_by=actor, status=TaskStatus.PENDING, schedule_id=schedule_id,
                  retry_of_id=retry_of, stats={}, log=[])
    db.add(run)
    db.flush()
    audit.record(db, actor=actor, action="scan.create", entity_type="scan_run", entity_id=run.id, details={"kind": kind, "scope": scope})
    db.commit()
    return run


def _claim(db: Session, scan_id: uuid.UUID) -> bool:
    res = db.execute(update(ScanRun).where(ScanRun.id == scan_id, ScanRun.status.in_([TaskStatus.PENDING, TaskStatus.RETRYING]))
                     .values(status=TaskStatus.RUNNING, started_at=datetime.now(timezone.utc), attempts=ScanRun.attempts + 1))
    db.commit()
    return res.rowcount == 1


def _check_cancel(db: Session, scan_id: uuid.UUID) -> None:
    db.expire_all()
    run = db.get(ScanRun, scan_id)
    if run.cancel_requested:
        raise ScanCancelled()


def run_scan(db: Session, scan_id: uuid.UUID) -> ScanRun:
    if not _claim(db, scan_id):
        return db.get(ScanRun, scan_id)
    run = db.get(ScanRun, scan_id)
    s = get_settings()
    log: list[str] = []
    stats: dict = {}
    try:
        _check_cancel(db, scan_id)
        stats.update(detect_gaps(db, run.scope or {}, actor=f"scan:{run.id}", scan_id=run.id, cap=s.learning_max_gaps_per_scan))
        log.append(f"coverage scan: {stats['lessons_scanned']} lesson(s), {stats['gaps_opened']} new gap(s)")
        db.commit()
        _check_cancel(db, scan_id)
        stats["source_changes"] = detect_source_changes(db, actor=f"scan:{run.id}", scan_id=run.id)
        stats["new_conflicts"] = detect_conflicts(db, actor=f"scan:{run.id}", scan_id=run.id)
        db.commit()
        _check_cancel(db, scan_id)
        stats["research_queued"] = plan_research(db, actor=f"scan:{run.id}", log=log)
        db.commit()
        _check_cancel(db, scan_id)
        assessed = 0
        for a in db.scalars(select(LearningAsset).where(LearningAsset.curriculum_node_id.is_not(None),
                                                         LearningAsset.status.in_(["APPROVED", "REVIEW"]))
                            .order_by(LearningAsset.updated_at).limit(s.learning_max_assets_assessed_per_scan)):
            quality.assess_asset(db, a.id, actor=f"scan:{run.id}")
            assessed += 1
        stats["assets_assessed"] = assessed
        stats["gaps_resolved"] = resolve_covered_gaps(db, run.scope or {}, actor=f"scan:{run.id}")
        run = db.get(ScanRun, scan_id)
        run.status, run.finished_at, run.stats, run.log, run.last_error = TaskStatus.SUCCEEDED, datetime.now(timezone.utc), stats, log, None
        db.commit()
    except ScanCancelled:
        db.rollback()
        run = db.get(ScanRun, scan_id)
        run.status, run.finished_at, run.stats, run.log = TaskStatus.CANCELLED, datetime.now(timezone.utc), stats, [*log, "cancelled on request"]
        db.commit()
    except Exception as exc:
        db.rollback()
        run = db.get(ScanRun, scan_id)
        run.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
        run.log = [*log, f"failed: {run.last_error}"]
        if run.attempts >= run.max_attempts:
            run.status, run.finished_at = TaskStatus.DEAD_LETTER, datetime.now(timezone.utc)
        else:
            run.status = TaskStatus.RETRYING
        db.commit()
    return run


def resolve_covered_gaps(db: Session, scope: dict, *, actor: str) -> int:
    """A lesson-bound gap closes only when the lesson is now COMPLETE (approved content), never merely because a draft exists."""
    resolved = 0
    for gap in db.scalars(select(KnowledgeGap).where(KnowledgeGap.status.in_(OPEN_STATES), KnowledgeGap.node_id.is_not(None))):
        node = db.get(CurriculumNode, gap.node_id)
        if node is None or node.deleted_at is not None or node.level != "LESSON":
            continue
        cur = cur_svc.get_curriculum(db, node.curriculum_id)
        subj = cur_svc.path_of(cur_svc.load_nodes(db, cur.id), node).get("SUBJECT")
        if coverage.lesson_state(db, node, subj.name if subj else "")["status"] == "COMPLETE":
            gap.status, gap.resolved_at, gap.resolution_note = "RESOLVED", datetime.now(timezone.utc), "lesson is COMPLETE with approved content"
            audit.record(db, actor=actor, action="gap.resolve", entity_type="knowledge_gap", entity_id=gap.id, details={"auto": True})
            resolved += 1
    return resolved


def cancel_scan(db: Session, scan_id: uuid.UUID, *, actor: str) -> ScanRun:
    run = db.get(ScanRun, scan_id)
    if run is None:
        raise NotFoundError("scan not found")
    if run.status in (TaskStatus.PENDING, TaskStatus.RETRYING):
        run.status, run.finished_at = TaskStatus.CANCELLED, datetime.now(timezone.utc)
    elif run.status == TaskStatus.RUNNING:
        run.cancel_requested = True            # honoured between phases
    else:
        raise ConflictError(f"scan is already {run.status.value}")
    audit.record(db, actor=actor, action="scan.cancel", entity_type="scan_run", entity_id=run.id, details={"status": run.status.value})
    db.commit()
    return run


def retry_scan(db: Session, scan_id: uuid.UUID, *, actor: str) -> ScanRun:
    old = db.get(ScanRun, scan_id)
    if old is None:
        raise NotFoundError("scan not found")
    if old.status not in (TaskStatus.DEAD_LETTER, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise ConflictError(f"only failed or cancelled scans can be retried (this one is {old.status.value})")
    return create_scan(db, old.scope, actor=actor, kind="RETRY", schedule_id=old.schedule_id, retry_of=old.id)


def pending_scan_ids(db: Session, limit: int = 3) -> list[uuid.UUID]:
    return list(db.scalars(select(ScanRun.id).where(ScanRun.status.in_([TaskStatus.PENDING, TaskStatus.RETRYING]),
                                                    ScanRun.attempts < ScanRun.max_attempts)
                           .order_by(ScanRun.priority, ScanRun.scheduled_at).limit(limit)))


# ------------------------------------------------------------------ schedules
def create_schedule(db: Session, *, name: str, scope: dict, interval_hours: int, actor: str, enabled: bool = True) -> ScanSchedule:
    if interval_hours < 1 or interval_hours > 24 * 90:
        raise InvalidInputError("interval_hours must be between 1 and 2160")
    if db.scalar(select(ScanSchedule).where(ScanSchedule.name == name)):
        raise ConflictError("a schedule with this name exists")
    row = ScanSchedule(name=name, scope=scope, interval_hours=interval_hours, enabled=enabled, created_by=actor,
                       next_run_at=datetime.now(timezone.utc) + timedelta(hours=interval_hours))
    db.add(row)
    db.flush()
    audit.record(db, actor=actor, action="schedule.create", entity_type="scan_schedule", entity_id=row.id,
                 details={"name": name, "interval_hours": interval_hours})
    return row


def run_due_schedules(db: Session, now: datetime | None = None) -> list[uuid.UUID]:
    """Create one SCHEDULED scan per due schedule (never two concurrent scans for the same schedule)."""
    now = now or datetime.now(timezone.utc)
    created = []
    for sch in db.scalars(select(ScanSchedule).where(ScanSchedule.enabled.is_(True), ScanSchedule.next_run_at <= now)):
        busy = db.scalar(select(ScanRun.id).where(ScanRun.schedule_id == sch.id, ScanRun.status.in_(ACTIVE_SCAN)))
        sch.next_run_at = now + timedelta(hours=sch.interval_hours)
        if busy:
            continue
        run = create_scan(db, sch.scope, actor=f"schedule:{sch.name}", kind="SCHEDULED", schedule_id=sch.id)
        sch.last_run_at = now
        created.append(run.id)
    db.commit()
    return created
