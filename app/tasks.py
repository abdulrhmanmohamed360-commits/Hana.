
"""Persistent background tasks for EduForge Knowledge Core."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.celery_app import celery_app
from app.db.session import build_engine
from app.models.enums import TaskStatus


settings = get_settings()


@celery_app.task(
    bind=True,
    name="app.tasks.process_ingestion",
    max_retries=settings.ingestion_max_retries,
    soft_time_limit=settings.ingestion_soft_time_limit_seconds,
    time_limit=settings.ingestion_time_limit_seconds,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_ingestion(self, job_id: str) -> dict:
    """Extract, OCR (when needed), chunk, embed and save one uploaded document.

    Heavy work runs here in the worker only. The job row is claimed atomically with a lease, so a redelivered
    message or a second worker cannot process the same job twice, and a crashed worker's job is recovered by
    `recover_ingestion_jobs` once its lease expires."""
    from app.services.embeddings import build_embedder
    from app.services.ingestion_pipeline import IngestionFailure, process_job, record_failure
    from app.services.storage import download_object

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    uid = uuid.UUID(job_id)

    try:
        with Session() as db:
            try:
                return process_job(
                    db,
                    uid,
                    settings,
                    downloader=download_object,
                    embedder=build_embedder(settings),
                )
            except Exception as exc:  # includes SoftTimeLimitExceeded (retryable, bounded)
                status = record_failure(
                    db,
                    uid,
                    exc,
                    retry_allowed=self.request.retries < self.max_retries,
                )
                if status == TaskStatus.RETRYING.value:
                    raise self.retry(
                        exc=exc,
                        countdown=min(30 * (2 ** self.request.retries), 300),
                    )
                if isinstance(exc, IngestionFailure) or status in {
                    TaskStatus.FAILED.value,
                    TaskStatus.DEAD_LETTER.value,
                }:
                    return {
                        "status": status.lower(),
                        "job_id": job_id,
                        "error_code": getattr(exc, "code", type(exc).__name__),
                    }
                raise
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.recover_ingestion_jobs")
def recover_ingestion_jobs() -> dict:
    """Re-queue ingestion jobs whose worker died (expired lease) or that were never dispatched."""
    from app.services.ingestion_pipeline import recover_stale_jobs

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with Session() as db:
            outcome = recover_stale_jobs(db, settings)

        for job_id in outcome["requeue"]:
            process_ingestion.apply_async(args=[str(job_id)], queue="ingestion")

        return {
            "requeued": len(outcome["requeue"]),
            "dead_lettered": outcome["dead_lettered"],
            "stale_found": outcome["stale_found"],
        }
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.scan_knowledge_gaps")
def scan_knowledge_gaps() -> dict:
    """Periodic curriculum coverage scan."""
    from app.services.growth import create_research_tasks

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with Session() as db:
            created = create_research_tasks(
                db,
                actor="system:auto-growth",
            )

            return {
                "created_tasks": len(created),
                "task_ids": [str(item.id) for item in created],
            }
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.process_pending_research")
def process_pending_research(batch_size: int = 5) -> dict:
    """Drain a bounded batch of persistent research tasks."""
    from app.models.ops import ResearchTask
    from app.services.growth import execute_research_task

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    processed = []

    try:
        with Session() as db:
            pending = db.scalars(
                select(ResearchTask)
                .where(
                    ResearchTask.status.in_([
                        TaskStatus.PENDING,
                        TaskStatus.RETRYING,
                    ]),
                    ResearchTask.attempts < ResearchTask.max_attempts,
                )
                .order_by(
                    ResearchTask.priority.asc(),
                    ResearchTask.scheduled_at.asc(),
                )
                .limit(max(1, min(batch_size, 20)))
            ).all()

            for item in pending:
                try:
                    result = execute_research_task(db, item)

                    processed.append({
                        "task_id": str(item.id),
                        "status": result["status"],
                    })

                except Exception as exc:
                    processed.append({
                        "task_id": str(item.id),
                        "status": "FAILED",
                        "error": type(exc).__name__,
                    })

        return {
            "processed": len(processed),
            "tasks": processed,
        }

    finally:
        engine.dispose()


@celery_app.task(
    name="app.tasks.run_preparation_job",
    soft_time_limit=900,
    time_limit=960,
)
def run_preparation_job(job_id: str) -> dict:
    """Execute one persistent preparation job."""
    from app.agents.llm import build_llm
    from app.services.embeddings import build_embedder
    from app.services.preparation_jobs import run_job

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with Session() as db:
            job = run_job(
                db,
                uuid.UUID(job_id),
                llm=build_llm(settings),
                embedder=build_embedder(settings),
            )

            return {
                "job_id": job_id,
                "status": job.status.value if job else "missing",
            }

    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.process_pending_preparations")
def process_pending_preparations(batch_size: int = 3) -> dict:
    """Recover and dispatch pending or stale preparation jobs."""
    from app.services.preparation_jobs import (
        pending_job_ids,
        requeue_stale,
    )

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with Session() as db:
            recovered = requeue_stale(db)
            ids = pending_job_ids(db, batch_size)

        for job_id in ids:
            run_preparation_job.delay(str(job_id))

        return {
            "recovered_stale": recovered,
            "dispatched": len(ids),
        }

    finally:
        engine.dispose()


@celery_app.task(
    name="app.tasks.run_learning_scan",
    soft_time_limit=1500,
    time_limit=1560,
)
def run_learning_scan(scan_id: str) -> dict:
    """Execute one continuous-learning scan."""
    from app.services.continuous_learning import run_scan

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with Session() as db:
            run = run_scan(db, uuid.UUID(scan_id))

            return {
                "scan_id": scan_id,
                "status": run.status.value if run else "missing",
            }

    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.process_learning_schedules")
def process_learning_schedules() -> dict:
    """Create scans for due schedules and dispatch pending scans."""
    from app.services.continuous_learning import (
        pending_scan_ids,
        run_due_schedules,
    )

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with Session() as db:
            created = run_due_schedules(db)
            ids = pending_scan_ids(db)

        for scan_id in ids:
            run_learning_scan.delay(str(scan_id))

        return {
            "scheduled_created": len(created),
            "dispatched": len(ids),
        }

    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.embed_pending_chunks")
def embed_pending_chunks_task(batch_size: int = 64) -> dict:
    """Embed chunks that have no vector yet."""
    from app.services.embeddings import (
        build_embedder,
        embed_pending_chunks,
    )

    embedder = build_embedder(settings)

    if embedder is None:
        return {
            "embedded": 0,
            "skipped": "embedding provider not configured",
        }

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with Session() as db:
            return {
                "embedded": embed_pending_chunks(
                    db,
                    embedder,
                    batch_size,
                )
            }

    finally:
        engine.dispose()
                
