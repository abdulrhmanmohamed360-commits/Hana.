from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, get_job_dispatcher, get_scan_dispatcher, require_role
from app.core.errors import NotFoundError
from app.models import KnowledgeGap, ScanRun, ScanSchedule
from app.models.enums import TaskStatus
from app.schemas.stage9 import GapUpdate, ScanScope, ScheduleCreate, SchedulePatch
from app.services import audit
from app.services import continuous_learning as svc

router = APIRouter(prefix="/continuous-learning", tags=["continuous-learning"], dependencies=[Depends(enforce_rate_limit)])


def _gap(g: KnowledgeGap) -> dict:
    return {"id": str(g.id), "type": g.gap_type, "status": g.status, "priority": g.priority, "description": g.description,
            "node_id": str(g.node_id) if g.node_id else None, "asset_id": str(g.asset_id) if g.asset_id else None,
            "detail": g.detail, "research_task_id": str(g.research_task_id) if g.research_task_id else None,
            "preparation_job_id": str(g.preparation_job_id) if g.preparation_job_id else None, "created_at": g.created_at,
            "resolved_at": g.resolved_at, "resolution_note": g.resolution_note}


def _scan(r: ScanRun) -> dict:
    return {"id": str(r.id), "kind": r.kind, "status": r.status.value, "scope": r.scope, "stats": r.stats, "log": r.log,
            "attempts": r.attempts, "max_attempts": r.max_attempts, "last_error": r.last_error,
            "cancel_requested": r.cancel_requested, "retry_of_id": str(r.retry_of_id) if r.retry_of_id else None,
            "created_at": r.created_at, "started_at": r.started_at, "finished_at": r.finished_at}


def _sch(s: ScanSchedule) -> dict:
    return {"id": str(s.id), "name": s.name, "scope": s.scope, "interval_hours": s.interval_hours, "enabled": s.enabled,
            "last_run_at": s.last_run_at, "next_run_at": s.next_run_at}


@router.get("/gaps")
def list_gaps(status: str | None = None, gap_type: str | None = None, node_id: uuid.UUID | None = None,
              limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db),
              _: Principal = Depends(require_role(Role.EDITOR))):
    stmt = select(KnowledgeGap).order_by(KnowledgeGap.priority, KnowledgeGap.created_at.desc()).limit(limit)
    for col, val in ((KnowledgeGap.status, status and status.upper()), (KnowledgeGap.gap_type, gap_type and gap_type.upper()), (KnowledgeGap.node_id, node_id)):
        if val:
            stmt = stmt.where(col == val)
    return [_gap(g) for g in db.scalars(stmt)]


@router.get("/gaps/{gap_id}")
def get_gap(gap_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.EDITOR))):
    g = db.get(KnowledgeGap, gap_id)
    if g is None:
        raise NotFoundError("gap not found")
    return _gap(g)


@router.patch("/gaps/{gap_id}")
def update_gap(gap_id: uuid.UUID, body: GapUpdate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    from datetime import datetime, timezone
    g = db.get(KnowledgeGap, gap_id)
    if g is None:
        raise NotFoundError("gap not found")
    g.status, g.resolution_note = body.status, body.note
    g.resolved_at = datetime.now(timezone.utc) if body.status in ("DISMISSED", "RESOLVED") else None
    audit.record(db, actor=who.actor, action="gap.update", entity_type="knowledge_gap", entity_id=g.id, details={"status": body.status, "note": body.note})
    db.commit()
    return _gap(g)


@router.post("/gaps/{gap_id}/prepare", status_code=202)
def prepare_gap(gap_id: uuid.UUID, db: Session = Depends(get_db), dispatch=Depends(get_job_dispatcher),
                who: Principal = Depends(require_role(Role.EDITOR))):
    """Queue DRAFT content for this gap through the preparation workflow. Result goes to human review, never to APPROVED."""
    job, dup = svc.prepare_for_gap(db, gap_id, actor=who.actor)
    if not dup:
        try:
            dispatch(str(job.id))
        except Exception:  # broker down: persisted job is re-dispatched by the beat safety net
            pass
    return {"job_id": str(job.id), "status": job.status.value, "status_url": f"/api/v1/knowledge/jobs/{job.id}", "deduplicated": dup}


@router.post("/scans", status_code=202)
def start_scan(scope: ScanScope = ScanScope(), db: Session = Depends(get_db), dispatch=Depends(get_scan_dispatcher),
               who: Principal = Depends(require_role(Role.EDITOR))):
    run = svc.create_scan(db, scope.clean(), actor=who.actor, kind="MANUAL")
    return _dispatch_scan(run, dispatch)


def _dispatch_scan(run: ScanRun, dispatch) -> dict:
    try:
        dispatch(str(run.id))
    except Exception:  # persisted; the beat task will pick it up
        pass
    return _scan(run)


@router.get("/scans")
def list_scans(status: str | None = None, limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db),
               _: Principal = Depends(require_role(Role.EDITOR))):
    stmt = select(ScanRun).order_by(ScanRun.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(ScanRun.status == TaskStatus(status.upper()))
    return [_scan(r) for r in db.scalars(stmt)]


@router.get("/scans/{scan_id}")
def get_scan(scan_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.EDITOR))):
    r = db.get(ScanRun, scan_id)
    if r is None:
        raise NotFoundError("scan not found")
    return _scan(r)


@router.post("/scans/{scan_id}/cancel")
def cancel_scan(scan_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    return _scan(svc.cancel_scan(db, scan_id, actor=who.actor))


@router.post("/scans/{scan_id}/retry", status_code=202)
def retry_scan(scan_id: uuid.UUID, db: Session = Depends(get_db), dispatch=Depends(get_scan_dispatcher),
               who: Principal = Depends(require_role(Role.EDITOR))):
    return _dispatch_scan(svc.retry_scan(db, scan_id, actor=who.actor), dispatch)


@router.get("/schedules")
def list_schedules(db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.EDITOR))):
    return [_sch(s) for s in db.scalars(select(ScanSchedule).order_by(ScanSchedule.name))]


@router.post("/schedules", status_code=201)
def create_schedule(body: ScheduleCreate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.ADMIN))):
    row = svc.create_schedule(db, name=body.name, scope=body.scope.clean(), interval_hours=body.interval_hours, enabled=body.enabled, actor=who.actor)
    db.commit()
    return _sch(row)


@router.patch("/schedules/{schedule_id}")
def patch_schedule(schedule_id: uuid.UUID, body: SchedulePatch, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.ADMIN))):
    row = db.get(ScanSchedule, schedule_id)
    if row is None:
        raise NotFoundError("schedule not found")
    if body.interval_hours is not None:
        row.interval_hours = body.interval_hours
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.scope is not None:
        row.scope = body.scope.clean()
    audit.record(db, actor=who.actor, action="schedule.update", entity_type="scan_schedule", entity_id=row.id, details=body.model_dump(mode="json", exclude_none=True))
    db.commit()
    return _sch(row)


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.ADMIN))):
    row = db.get(ScanSchedule, schedule_id)
    if row is None:
        raise NotFoundError("schedule not found")
    audit.record(db, actor=who.actor, action="schedule.delete", entity_type="scan_schedule", entity_id=row.id, details={"name": row.name})
    db.delete(row)
    db.commit()
