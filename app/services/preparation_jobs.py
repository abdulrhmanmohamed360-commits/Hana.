"""Persistent execution of preparation jobs: dedupe, atomic claim, progress, retry, dead-letter, stale-job recovery."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.llm import StructuredLLM
from app.core.config import get_settings
from app.models import PreparationJob
from app.models.enums import TaskStatus
from app.schemas.prepare import PrepareRequest
from app.services import audit
from app.services.embeddings import Embedder
from app.services.prepare import PrepareContext, build_package, request_hash

ACTIVE = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING)
STALE_AFTER = timedelta(minutes=30)


def create_or_get_job(db: Session, req: PrepareRequest, actor: str) -> tuple[PreparationJob, bool]:
    """Return (job, deduplicated). An identical request that is still active is reused, not queued twice."""
    h = request_hash(req)
    existing = db.scalar(select(PreparationJob).where(PreparationJob.request_hash == h,
                                                      PreparationJob.status.in_(ACTIVE))
                         .order_by(PreparationJob.created_at.desc()))
    if existing is not None:
        return existing, True
    job = PreparationJob(request=req.model_dump(mode="json"), request_hash=h, created_by=actor,
                         status=TaskStatus.PENDING, max_attempts=get_settings().prepare_job_max_attempts,
                         progress={"stage": "queued", "percent": 0, "note": "waiting for a worker"})
    db.add(job)
    db.flush()
    audit.record(db, actor=actor, action="prepare.job_created", entity_type="preparation_job", entity_id=job.id,
                 details={"lesson": req.lesson, "subject": req.subject, "grade": req.grade})
    db.commit()
    return job, False


def _claim(db: Session, job_id: uuid.UUID) -> bool:
    """Atomic PENDING/RETRYING -> RUNNING so two workers can never execute the same job."""
    res = db.execute(update(PreparationJob)
                     .where(PreparationJob.id == job_id,
                            PreparationJob.status.in_([TaskStatus.PENDING, TaskStatus.RETRYING]))
                     .values(status=TaskStatus.RUNNING, started_at=datetime.now(timezone.utc),
                             attempts=PreparationJob.attempts + 1))
    db.commit()
    return res.rowcount == 1


def run_job(db: Session, job_id: uuid.UUID, *, llm: StructuredLLM | None = None, embedder: Embedder | None = None,
            research_runner: Callable | None = None) -> PreparationJob | None:
    if not _claim(db, job_id):
        return db.get(PreparationJob, job_id)
    job = db.get(PreparationJob, job_id)
    db.refresh(job)

    def progress(stage: str, percent: int, note: str) -> None:
        job.progress = {"stage": stage, "percent": percent, "note": note,
                        "updated_at": datetime.now(timezone.utc).isoformat()}
        db.commit()

    try:
        req = PrepareRequest.model_validate(job.request)
        ctx = PrepareContext(actor=job.created_by, llm=llm, embedder=embedder, allow_llm=llm is not None,
                             research_runner=research_runner, progress=progress)
        package = build_package(db, req, ctx)
        job.result = package.model_dump(mode="json")
        job.status, job.last_error = TaskStatus.SUCCEEDED, None
        job.finished_at = datetime.now(timezone.utc)
        job.progress = {"stage": "done", "percent": 100, "note": f"package status {package.status}"}
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(PreparationJob, job_id)
        job.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
        if job.attempts >= job.max_attempts:
            job.status, job.finished_at = TaskStatus.DEAD_LETTER, datetime.now(timezone.utc)
            job.progress = {**(job.progress or {}), "note": "failed permanently"}
        else:
            job.status = TaskStatus.RETRYING
        db.commit()
    return job


def requeue_stale(db: Session) -> int:
    """A worker that died mid-run leaves status RUNNING forever; hand such jobs back (or dead-letter them)."""
    cutoff = datetime.now(timezone.utc) - STALE_AFTER
    stale = db.scalars(select(PreparationJob).where(PreparationJob.status == TaskStatus.RUNNING,
                                                    PreparationJob.started_at < cutoff)).all()
    for job in stale:
        job.last_error = "worker stopped before finishing (stale RUNNING job)"
        job.status = TaskStatus.DEAD_LETTER if job.attempts >= job.max_attempts else TaskStatus.RETRYING
        if job.status == TaskStatus.DEAD_LETTER:
            job.finished_at = datetime.now(timezone.utc)
    db.commit()
    return len(stale)


def pending_job_ids(db: Session, limit: int = 5) -> list[uuid.UUID]:
    return list(db.scalars(select(PreparationJob.id).where(
        PreparationJob.status.in_([TaskStatus.PENDING, TaskStatus.RETRYING]),
        PreparationJob.attempts < PreparationJob.max_attempts)
        .order_by(PreparationJob.priority, PreparationJob.scheduled_at).limit(max(1, min(limit, 20)))))
