"""Celery application and persistent ingestion worker."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from celery import Celery
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import build_engine
from app.models import IngestionJob, KnowledgeChunk, RawDocument, Source
from app.models.enums import DocumentStatus, SourcePolicy, TaskStatus
from app.services.ingestion import content_hash, extract_segments
from app.services.chunking import TextSegment, chunk_segments

settings = get_settings()
celery_app = Celery("eduforge", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    broker_connection_retry_on_startup=True,
    task_routes={"app.tasks.process_ingestion": {"queue": "ingestion"},
                 "app.tasks.run_preparation_job": {"queue": "research"},
                 "app.tasks.run_learning_scan": {"queue": "research"}},
)


@celery_app.task(bind=True, name="app.tasks.process_ingestion", max_retries=2)
def process_ingestion(self, job_id: str) -> dict:
    from sqlalchemy.orm import sessionmaker

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session() as db:
            job = db.get(IngestionJob, uuid.UUID(job_id))
            if job is None:
                return {"status": "missing", "job_id": job_id}
            if job.status == TaskStatus.SUCCEEDED:
                return {"status": "already_processed", "job_id": job_id}
            job.status = TaskStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            job.attempts += 1
            db.commit()
            try:
                source = db.get(Source, job.source_id) if job.source_id else None
                if source and source.policy != SourcePolicy.ALLOWED:
                    raise ValueError("Source is not approved for ingestion")
                file_path = Path(job.input_uri or "")
                if not file_path.is_file():
                    raise ValueError("Stored upload is missing")
                original_name = (job.meta or {}).get("original_name", file_path.name)
                extracted_segments, mime = extract_segments(file_path, original_name)
                extracted = "\n\n".join(text for text, _ in extracted_segments)
                digest = content_hash(extracted)
                existing = db.scalar(select(RawDocument).where(RawDocument.content_hash == digest))
                if existing:
                    # Backfill chunks for documents ingested by an earlier version of the pipeline.
                    has_chunks = db.scalar(select(KnowledgeChunk.id).where(
                        KnowledgeChunk.document_id == existing.id).limit(1))
                    chunks_created = 0
                    if not has_chunks:
                        chunks = chunk_segments([TextSegment(text, page) for text, page in extracted_segments])
                        for chunk in chunks:
                            db.add(KnowledgeChunk(
                                document_id=existing.id, domain_id=existing.domain_id, topic_id=existing.topic_id,
                                chunk_index=chunk.index, page_start=chunk.page_start, page_end=chunk.page_end,
                                content=chunk.content, content_hash=chunk.content_hash,
                                language=existing.language or "und", token_estimate=chunk.token_estimate,
                                meta={"backfilled_by_ingestion_job_id": str(job.id)},
                            ))
                        chunks_created = len(chunks)
                    job.documents_created = 0
                    job.duplicates_skipped = 1
                    job.status = TaskStatus.SUCCEEDED
                    job.finished_at = datetime.now(timezone.utc)
                    db.commit()
                    return {"status": "duplicate", "job_id": job_id, "document_id": str(existing.id),
                            "chunks_created": chunks_created}
                doc = RawDocument(
                    source_id=job.source_id,
                    title=(job.meta or {}).get("original_name"),
                    original_url=(job.meta or {}).get("original_url"),
                    mime_type=mime,
                    content_hash=digest,
                    text_content=extracted,
                    status=DocumentStatus.EXTRACTED,
                    meta={"storage_path": str(file_path), "original_name": original_name,
                          "file_sha256": (job.meta or {}).get("file_sha256"), "character_count": len(extracted)},
                )
                db.add(doc)
                db.flush()
                chunks = chunk_segments([TextSegment(text, page) for text, page in extracted_segments])
                for chunk in chunks:
                    db.add(KnowledgeChunk(
                        document_id=doc.id, domain_id=doc.domain_id, topic_id=doc.topic_id,
                        chunk_index=chunk.index, page_start=chunk.page_start, page_end=chunk.page_end,
                        content=chunk.content, content_hash=chunk.content_hash,
                        language=doc.language or "und", token_estimate=chunk.token_estimate,
                        meta={"ingestion_job_id": str(job.id)},
                    ))
                job.documents_created = 1
                job.duplicates_skipped = 0
                job.status = TaskStatus.SUCCEEDED
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
                return {"status": "created", "job_id": job_id, "document_id": str(doc.id), "characters": len(extracted), "chunks_created": len(chunks)}
            except Exception as exc:
                db.rollback()
                job = db.get(IngestionJob, uuid.UUID(job_id))
                job.attempts = max(job.attempts, self.request.retries + 1)
                job.last_error = str(exc)[:4000]
                if self.request.retries < self.max_retries:
                    job.status = TaskStatus.RETRYING
                    db.commit()
                    raise self.retry(exc=exc, countdown=min(30 * (2 ** self.request.retries), 300))
                job.status = TaskStatus.DEAD_LETTER
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
                raise
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.scan_knowledge_gaps")
def scan_knowledge_gaps() -> dict:
    """Periodic curriculum coverage scan; creates persistent, idempotent research tasks."""
    from sqlalchemy.orm import sessionmaker
    from app.services.growth import create_research_tasks

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session() as db:
            created = create_research_tasks(db, actor="system:auto-growth")
            return {"created_tasks": len(created), "task_ids": [str(item.id) for item in created]}
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.process_pending_research")
def process_pending_research(batch_size: int = 5) -> dict:
    """Drain a bounded batch of persistent research tasks from the database queue."""
    from sqlalchemy.orm import sessionmaker
    from app.models.ops import ResearchTask
    from app.services.growth import execute_research_task

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    processed = []
    try:
        with Session() as db:
            pending = db.scalars(
                select(ResearchTask)
                .where(ResearchTask.status.in_([TaskStatus.PENDING, TaskStatus.RETRYING]),
                       ResearchTask.attempts < ResearchTask.max_attempts)
                .order_by(ResearchTask.priority.asc(), ResearchTask.scheduled_at.asc())
                .limit(max(1, min(batch_size, 20)))
            ).all()
            for item in pending:
                try:
                    result = execute_research_task(db, item)
                    processed.append({"task_id": str(item.id), "status": result["status"]})
                except Exception as exc:
                    processed.append({"task_id": str(item.id), "status": "FAILED",
                                      "error": type(exc).__name__})
        return {"processed": len(processed), "tasks": processed}
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.run_preparation_job", soft_time_limit=900, time_limit=960)
def run_preparation_job(job_id: str) -> dict:
    """Execute one persistent preparation job (retries/dead-letter are tracked in the job row itself)."""
    from sqlalchemy.orm import sessionmaker
    from app.agents.llm import build_llm
    from app.services.embeddings import build_embedder
    from app.services.preparation_jobs import run_job

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session() as db:
            job = run_job(db, uuid.UUID(job_id), llm=build_llm(settings), embedder=build_embedder(settings))
            return {"job_id": job_id, "status": job.status.value if job else "missing"}
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.process_pending_preparations")
def process_pending_preparations(batch_size: int = 3) -> dict:
    """Safety net: picks up jobs whose enqueue failed, RETRYING jobs, and recovers stale RUNNING jobs."""
    from sqlalchemy.orm import sessionmaker
    from app.services.preparation_jobs import pending_job_ids, requeue_stale

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session() as db:
            recovered = requeue_stale(db)
            ids = pending_job_ids(db, batch_size)
        for job_id in ids:
            run_preparation_job.delay(str(job_id))
        return {"recovered_stale": recovered, "dispatched": len(ids)}
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.run_learning_scan", soft_time_limit=1500, time_limit=1560)
def run_learning_scan(scan_id: str) -> dict:
    """Execute one continuous-learning scan (history, retry and cancellation live in the scan_runs row)."""
    from sqlalchemy.orm import sessionmaker
    from app.services.continuous_learning import run_scan

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session() as db:
            run = run_scan(db, uuid.UUID(scan_id))
            return {"scan_id": scan_id, "status": run.status.value if run else "missing"}
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.process_learning_schedules")
def process_learning_schedules() -> dict:
    """Beat: create scans for due schedules and dispatch pending/retrying scans (also recovers failed enqueues)."""
    from sqlalchemy.orm import sessionmaker
    from app.services.continuous_learning import pending_scan_ids, run_due_schedules

    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session() as db:
            created = run_due_schedules(db)
            ids = pending_scan_ids(db)
        for sid in ids:
            run_learning_scan.delay(str(sid))
        return {"scheduled_created": len(created), "dispatched": len(ids)}
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.embed_pending_chunks")
def embed_pending_chunks_task(batch_size: int = 64) -> dict:
    """Embed chunks that have no vector yet (no-op when no embedding provider is configured)."""
    from sqlalchemy.orm import sessionmaker
    from app.services.embeddings import build_embedder, embed_pending_chunks

    embedder = build_embedder(settings)
    if embedder is None:
        return {"embedded": 0, "skipped": "embedding provider not configured"}
    engine = build_engine(settings.database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session() as db:
            return {"embedded": embed_pending_chunks(db, embedder, batch_size)}
    finally:
        engine.dispose()


celery_app.conf.beat_schedule = {
    "process-preparation-jobs-every-minute": {
        "task": "app.tasks.process_pending_preparations", "schedule": 60,
        "options": {"queue": "research"},
    },
    "learning-schedules-every-15-minutes": {
        "task": "app.tasks.process_learning_schedules", "schedule": 15 * 60,
        "options": {"queue": "research"},
    },
    "embed-pending-chunks-every-ten-minutes": {
        "task": "app.tasks.embed_pending_chunks", "schedule": 10 * 60,
        "options": {"queue": "research"},
    },
    "scan-knowledge-gaps-every-day": {
        "task": "app.tasks.scan_knowledge_gaps",
        "schedule": 24 * 60 * 60,
        "options": {"queue": "research"},
    },
    "process-research-queue-every-five-minutes": {
        "task": "app.tasks.process_pending_research",
        "schedule": 5 * 60,
        "options": {"queue": "research"},
    },
}
