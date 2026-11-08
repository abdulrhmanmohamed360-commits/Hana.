"""Document ingestion pipeline: claim -> validate -> fetch -> verify -> extract -> normalize -> chunk -> save -> embed.

DB-facing orchestration around the pure `docreader` engine. All heavy work (extraction, OCR) happens here, inside the
Celery worker. Every stage is recorded on the job so failures are traceable by job id."""
from __future__ import annotations

import re
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.models import Embedding, IngestionJob, KnowledgeChunk, RawDocument, Source
from app.models.enums import DocumentStatus, SourcePolicy, TaskStatus
from app.services.docreader import (ExtractionError, ReaderConfig, arabic_quality_summary, detect_language,
                                    extract_document, normalize_for_search, safe_filename, sha256_file)
from app.services.docreader.ocr import OcrEngine
from app.services.ingestion import content_hash, normalize_text
from app.services.structured_chunking import ChunkOut, chunk_result

log = get_logger("eduforge.ingestion")

STAGES = ("STARTING", "VALIDATING", "DOWNLOADING", "VERIFYING", "EXTRACTING", "NORMALIZING", "CHUNKING", "SAVING",
          "EMBEDDING", "DONE")
_SECRET_URL = re.compile(r"(https?://[^\s'\"]+?)(\?[^\s'\"]*)?(?=[\s'\"]|$)")
_TOKEN_PARAM = re.compile(r"(token|apikey|api_key|key|authorization|secret|password)=([^&\s]+)", re.IGNORECASE)
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)


class IngestionFailure(Exception):
    """Raised by the pipeline; carries a stable code, the stage, and whether a retry can help."""

    def __init__(self, message: str, *, code: str, stage: str, retryable: bool, details: dict | None = None):
        super().__init__(message)
        self.code, self.stage, self.retryable, self.details = code, stage, retryable, details or {}


def redact(message: str, limit: int = 1000) -> str:
    """Never let URLs with signed tokens, bearer tokens or keys reach job rows, logs or API responses."""
    message = _BEARER.sub("Bearer [redacted]", message or "")
    message = _TOKEN_PARAM.sub(r"\1=[redacted]", message)
    message = _SECRET_URL.sub(lambda m: m.group(1) + ("?[redacted]" if m.group(2) else ""), message)
    return message[:limit]


def reader_config_from_settings(settings: Settings) -> ReaderConfig:
    return ReaderConfig(
        max_file_bytes=settings.max_upload_bytes, max_pages=settings.ingestion_max_pages,
        max_chars=settings.ingestion_max_chars, min_page_chars=settings.pdf_min_page_chars,
        min_page_quality=settings.pdf_min_page_quality, ocr_enabled=settings.ocr_enabled,
        ocr_languages=settings.ocr_languages, ocr_dpi=settings.ocr_dpi, ocr_max_pages=settings.ocr_max_pages_per_document,
        ocr_page_timeout_seconds=settings.ocr_page_timeout_seconds, ocr_max_image_pixels=settings.ocr_max_image_pixels,
        office_max_uncompressed_bytes=settings.office_max_uncompressed_bytes,
        office_max_compression_ratio=settings.office_max_compression_ratio,
        office_max_members=settings.office_max_members, time_budget_seconds=settings.ingestion_document_budget_seconds)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------------------------ job state helpers
def claim_job(db: Session, job_id, lease_seconds: int) -> str:
    """Atomically take ownership. Returns 'claimed' | 'missing' | 'done' | 'busy' | 'exhausted'.

    Two workers (or a redelivered message after a crash) can never both run the same job: the UPDATE only matches
    jobs that are queued, or RUNNING with an expired lease."""
    job = db.get(IngestionJob, job_id)
    if job is None:
        return "missing"
    if job.status in (TaskStatus.SUCCEEDED, TaskStatus.CANCELLED):
        return "done"
    if job.attempts >= max(1, job.max_attempts) and job.status != TaskStatus.RUNNING:
        job.status = TaskStatus.DEAD_LETTER
        job.finished_at = _now()
        job.last_error = job.last_error or "Maximum processing attempts reached"
        db.commit()
        return "exhausted"
    now = _now()
    result = db.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id,
               or_(IngestionJob.status.in_([TaskStatus.PENDING, TaskStatus.RETRYING]),
                   and_(IngestionJob.status == TaskStatus.RUNNING,
                        or_(IngestionJob.lease_expires_at.is_(None), IngestionJob.lease_expires_at < now))))
        .values(status=TaskStatus.RUNNING, started_at=now, finished_at=None, attempts=IngestionJob.attempts + 1,
                lease_expires_at=now + timedelta(seconds=lease_seconds), stage="STARTING"))
    db.commit()
    if result.rowcount != 1:
        return "busy"
    db.refresh(job)
    return "claimed"


def _set_stage(db: Session, job: IngestionJob, stage: str, lease_seconds: int) -> None:
    job.stage = stage
    job.lease_expires_at = _now() + timedelta(seconds=lease_seconds)  # heartbeat: stage progress extends the lease
    db.commit()
    log.info("ingestion_stage", extra={"job_id": str(job.id), "stage": stage, "attempt": job.attempts})


def _merge_meta(job: IngestionJob, **values: Any) -> None:
    job.meta = {**(job.meta or {}), **values}  # reassign: in-place JSON mutation is not tracked


def record_failure(db: Session, job_id, exc: BaseException, *, retry_allowed: bool, stage: str | None = None) -> str:
    """Persist the error and move the job to RETRYING / FAILED / DEAD_LETTER. Returns the new status value."""
    db.rollback()
    job = db.get(IngestionJob, job_id)
    if job is None:
        return "missing"
    if isinstance(exc, IngestionFailure):
        code, retryable, stage = exc.code, exc.retryable, exc.stage
    elif isinstance(exc, ExtractionError):
        code, retryable = exc.code, exc.retryable
    else:
        code, retryable = type(exc).__name__.upper(), True  # unknown/infra errors are retried, boundedly
    message = redact(str(exc))
    errors = list((job.meta or {}).get("errors", []))[-19:]
    errors.append({"attempt": job.attempts, "stage": stage or job.stage, "code": code, "message": message,
                   "retryable": retryable, "at": _now().isoformat()})
    details = getattr(exc, "details", None) or {}
    _merge_meta(job, errors=errors, last_error_code=code, **({"last_error_details": details} if details else {}))
    job.last_error = f"[{code}] {message}"[:4000]
    job.lease_expires_at = None
    if retryable and retry_allowed and job.attempts < job.max_attempts:
        job.status = TaskStatus.RETRYING
    elif retryable:
        job.status = TaskStatus.DEAD_LETTER
        job.finished_at = _now()
    else:
        job.status = TaskStatus.FAILED
        job.finished_at = _now()
    db.commit()
    log.error("ingestion_failed", extra={"job_id": str(job.id), "stage": stage or job.stage, "code": code,
                                          "attempt": job.attempts, "status": job.status.value, "retryable": retryable})
    return job.status.value


# ------------------------------------------------------------------------------------------------ fetching
def _temp_dir(settings: Settings) -> Path:
    base = Path(settings.ingestion_temp_dir or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fetch_input(job: IngestionJob, settings: Settings, downloader: Callable, original_name: str) -> tuple[Path, bool, str]:
    """Returns (local_path, is_temporary, durable_storage_path). Private storage only; local paths must live inside
    the configured storage dir (no arbitrary filesystem reads)."""
    input_uri = job.input_uri or ""
    suffix = Path(original_name).suffix.lower()
    if input_uri.startswith("supabase://"):
        reference = input_uri[len("supabase://"):]
        bucket, sep, object_path = reference.partition("/")
        if not sep or not bucket or not object_path:
            raise IngestionFailure("Invalid storage reference", code="BAD_STORAGE_URI", stage="DOWNLOADING", retryable=False)
        if bucket != settings.supabase_storage_bucket:
            raise IngestionFailure("Unexpected storage bucket", code="BAD_STORAGE_BUCKET", stage="DOWNLOADING", retryable=False)
        handle = tempfile.NamedTemporaryFile(suffix=suffix, prefix="eduforge-", dir=_temp_dir(settings), delete=False)
        handle.close()
        destination = Path(handle.name)
        try:
            downloader(settings=settings, object_path=object_path, destination=destination,
                       max_bytes=settings.max_upload_bytes)
        except Exception as exc:
            destination.unlink(missing_ok=True)
            retryable = type(exc).__name__ != "FileTooLargeError"
            raise IngestionFailure(redact(str(exc)) or "Could not download the stored document", code="STORAGE_ERROR",
                                   stage="DOWNLOADING", retryable=retryable) from exc
        return destination, True, input_uri
    local = Path(input_uri).resolve()
    root = Path(settings.storage_dir).resolve()
    if root not in local.parents:
        raise IngestionFailure("Stored upload path is outside the storage directory", code="BAD_LOCAL_PATH",
                               stage="DOWNLOADING", retryable=False)
    if not local.is_file():
        raise IngestionFailure("Stored upload is missing", code="STORED_FILE_MISSING", stage="DOWNLOADING", retryable=False)
    return local, False, str(local)


# ------------------------------------------------------------------------------------------------ persistence
def _chunk_rows(doc: RawDocument, chunks: list[ChunkOut], job: IngestionJob, source_id) -> list[KnowledgeChunk]:
    rows = []
    for c in chunks:
        meta = {"ingestion_job_id": str(job.id), "kind": c.kind, **c.meta}
        rows.append(KnowledgeChunk(
            document_id=doc.id, domain_id=doc.domain_id, topic_id=doc.topic_id, source_id=source_id,
            chunk_index=c.index, page_start=c.page_start, page_end=c.page_end, content=c.content,
            content_hash=c.content_hash, language=c.language if c.language != "und" else (doc.language or "und"),
            token_estimate=c.token_estimate, content_norm=normalize_for_search(c.content),
            section_title=c.section_title, chapter_title=c.chapter_title, extraction_method=c.extraction_method[:32],
            extraction_confidence=c.extraction_confidence, meta=meta))
    return rows


def _existing_chunk_count(db: Session, document_id) -> int:
    return db.scalar(select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.document_id == document_id)) or 0


def embed_document_chunks(db: Session, embedder: Any, document_id, batch_size: int = 32) -> dict:
    """Embed only this document's chunks through the existing embedder. Never fails the job: the periodic
    `embed_pending_chunks` task backfills anything missed."""
    if embedder is None:
        return {"status": "skipped", "reason": "embedding provider not configured", "embedded": 0}
    done = select(Embedding.owner_id).where(Embedding.owner_type == "knowledge_chunk", Embedding.model == embedder.model)
    embedded = 0
    try:
        while True:
            pending = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id,
                                                              KnowledgeChunk.id.not_in(done))
                                 .order_by(KnowledgeChunk.chunk_index).limit(batch_size)).all()
            if not pending:
                break
            vectors = embedder.embed([(c.content_norm or c.content)[:8000] for c in pending])
            for chunk, vec in zip(pending, vectors):
                db.add(Embedding(owner_type="knowledge_chunk", owner_id=chunk.id, model=embedder.model,
                                 dimensions=len(vec), embedding=vec))
            db.commit()
            embedded += len(pending)
    except Exception as exc:
        db.rollback()
        log.warning("embedding_deferred", extra={"document_id": str(document_id), "error": type(exc).__name__})
        return {"status": "deferred", "reason": type(exc).__name__, "embedded": embedded}
    return {"status": "ok", "embedded": embedded}


def _finish(db: Session, job: IngestionJob, *, created: int, duplicates: int, stats: dict, document_id) -> None:
    _merge_meta(job, document_id=str(document_id), stats=stats, errors=(job.meta or {}).get("errors", []))
    job.documents_created = created
    job.duplicates_skipped = duplicates
    job.status = TaskStatus.SUCCEEDED
    job.stage = "DONE"
    job.finished_at = _now()
    job.lease_expires_at = None
    job.last_error = None
    db.commit()


# ------------------------------------------------------------------------------------------------ main entry
def process_job(db: Session, job_id, settings: Settings, *, downloader: Callable, embedder: Any = None,
                ocr: OcrEngine | None = None, config: ReaderConfig | None = None) -> dict:
    """Run one ingestion job end to end. Returns a result dict; raises IngestionFailure/other on failure
    (the caller decides on retry via `record_failure`). Idempotent: re-running never duplicates documents/chunks."""
    claim = claim_job(db, job_id, settings.ingestion_lease_seconds)
    if claim != "claimed":
        return {"status": {"missing": "missing", "done": "already_processed", "busy": "skipped_concurrent",
                           "exhausted": "dead_letter"}[claim], "job_id": str(job_id)}
    job = db.get(IngestionJob, job_id)
    lease = settings.ingestion_lease_seconds
    started = time.monotonic()
    config = config or reader_config_from_settings(settings)
    ocr = ocr or OcrEngine(config)
    temp_path: Path | None = None
    log.info("ingestion_started", extra={"job_id": str(job.id), "attempt": job.attempts})
    try:
        _set_stage(db, job, "VALIDATING", lease)
        meta = job.meta or {}
        original_name = safe_filename(meta.get("original_name"), "document.pdf")
        source = db.get(Source, job.source_id) if job.source_id else None
        if source is not None and source.policy != SourcePolicy.ALLOWED:
            raise IngestionFailure("Source is not approved for ingestion", code="SOURCE_NOT_ALLOWED",
                                   stage="VALIDATING", retryable=False)

        _set_stage(db, job, "DOWNLOADING", lease)
        file_path, is_temp, durable_path = _fetch_input(job, settings, downloader, original_name)
        temp_path = file_path if is_temp else None

        _set_stage(db, job, "VERIFYING", lease)
        file_digest = sha256_file(file_path)
        claimed_digest = (meta.get("file_sha256") or "").lower() or None
        if claimed_digest and claimed_digest != file_digest:
            raise IngestionFailure("Stored file does not match the declared checksum", code="INTEGRITY_MISMATCH",
                                   stage="VERIFYING", retryable=False)
        by_file = db.scalar(select(RawDocument).where(RawDocument.file_sha256 == file_digest,
                                                      RawDocument.deleted_at.is_(None)))
        if by_file is not None and _existing_chunk_count(db, by_file.id) > 0:
            return _duplicate_result(db, job, by_file, started, reason="same_file", embedder=embedder,
                                     batch=settings.embed_batch_size)

        _set_stage(db, job, "EXTRACTING", lease)
        try:
            result = extract_document(file_path, original_name, config, ocr)
        except ExtractionError as exc:
            raise IngestionFailure(redact(str(exc)), code=exc.code, stage="EXTRACTING", retryable=exc.retryable,
                                   details=exc.details) from exc

        _set_stage(db, job, "NORMALIZING", lease)
        original_text = result.full_text                       # original extracted text, never overwritten
        digest = content_hash(normalize_text(original_text))
        language = detect_language(original_text)
        by_text = db.scalar(select(RawDocument).where(RawDocument.content_hash == digest))
        if by_text is not None:
            if by_text.file_sha256 is None:
                by_text.file_sha256 = file_digest
            if _existing_chunk_count(db, by_text.id) > 0:
                return _duplicate_result(db, job, by_text, started, reason="same_content", embedder=embedder,
                                         batch=settings.embed_batch_size)

        _set_stage(db, job, "CHUNKING", lease)
        chunks = chunk_result(result, settings.chunk_max_chars, settings.chunk_overlap_chars)
        if not chunks:
            raise IngestionFailure("Extraction produced no usable chunks", code="NO_CHUNKS", stage="CHUNKING",
                                   retryable=False)

        stats = _build_stats(result, chunks, started, ocr, language)

        _set_stage(db, job, "SAVING", lease)
        try:
            if by_text is not None:                            # row exists but has no chunks (earlier partial run)
                doc, created = by_text, 0
                db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == doc.id).delete()
            else:
                doc = RawDocument(
                    source_id=job.source_id, title=original_name, original_url=meta.get("original_url"),
                    mime_type=result.mime, language=language, content_hash=digest, file_sha256=file_digest,
                    text_content=original_text, status=DocumentStatus.EXTRACTED,
                    meta={"storage_path": durable_path,
                          "storage_backend": "supabase" if durable_path.startswith("supabase://") else "local",
                          "original_name": original_name, "file_sha256": file_digest,
                          "character_count": len(original_text), "extraction": _doc_summary(stats, result)})
                db.add(doc)
                db.flush()
                created = 1
            db.add_all(_chunk_rows(doc, chunks, job, job.source_id))
            db.flush()
            _finish(db, job, created=created, duplicates=0, stats=stats, document_id=doc.id)  # commits atomically
        except IntegrityError:
            db.rollback()                                       # a concurrent job stored the same document first
            winner = db.scalar(select(RawDocument).where(RawDocument.content_hash == digest))
            if winner is None:
                raise
            job = db.get(IngestionJob, job_id)
            return _duplicate_result(db, job, winner, started, reason="concurrent_duplicate", embedder=embedder,
                                     batch=settings.embed_batch_size)

        _set_stage_quiet(db, job, "EMBEDDING")
        embedding = embed_document_chunks(db, embedder, doc.id, settings.embed_batch_size)
        job = db.get(IngestionJob, job_id)
        final_stats = {**stats, "embedding": embedding}
        _merge_meta(job, stats=final_stats)
        job.stage = "DONE"
        db.commit()
        log.info("ingestion_succeeded", extra={"job_id": str(job.id), "document_id": str(doc.id),
                                               "chunks": len(chunks), "pages": result.total_pages,
                                               "ocr_pages": result.ocr_pages, "method": result.primary_method,
                                               "duration_s": final_stats["duration_seconds"]})
        return {"status": "created" if created else "recovered", "job_id": str(job_id), "document_id": str(doc.id),
                "characters": len(original_text), "chunks_created": len(chunks), "stats": final_stats}
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _set_stage_quiet(db: Session, job: IngestionJob, stage: str) -> None:
    """Progress marker after the job is already durably SUCCEEDED (embedding is best-effort and backfilled)."""
    job.stage = stage
    db.commit()


def _duplicate_result(db: Session, job: IngestionJob, existing: RawDocument, started: float, *, reason: str,
                      embedder: Any, batch: int) -> dict:
    """Same file/content already stored with chunks: succeed without creating anything."""
    chunk_count = _existing_chunk_count(db, existing.id)
    stats = {"duplicate": True, "duplicate_reason": reason, "existing_document_id": str(existing.id),
             "chunks_created": 0, "chunks_existing": chunk_count,
             "duration_seconds": round(time.monotonic() - started, 3),
             **({"extraction_method": (existing.meta or {}).get("extraction", {}).get("extraction_method")}
                if (existing.meta or {}).get("extraction") else {})}
    prior = (existing.meta or {}).get("extraction") or {}
    for key in ("total_pages", "extracted_pages", "ocr_pages", "character_count", "failed_pages"):
        if key in prior:
            stats[key] = prior[key]
    _finish(db, job, created=0, duplicates=1, stats=stats, document_id=existing.id)
    log.info("ingestion_duplicate", extra={"job_id": str(job.id), "document_id": str(existing.id), "reason": reason})
    return {"status": "duplicate", "job_id": str(job.id), "document_id": str(existing.id), "chunks_created": 0}


def _build_stats(result, chunks: list[ChunkOut], started: float, ocr: OcrEngine, language: str) -> dict:
    return {
        "file_type": result.file_type, "language": language, "extraction_method": result.primary_method,
        "methods_used": result.methods_used, "total_pages": result.total_pages,
        "extracted_pages": result.extracted_pages, "ocr_pages": result.ocr_pages, "failed_pages": result.failed_pages,
        "blank_pages": [p.page for p in result.pages if p.blank and p.page is not None],
        "character_count": result.character_count, "chunks_created": len(chunks),
        "mean_confidence": result.mean_confidence, "arabic_quality": arabic_quality_summary(result),
        "extraction_seconds": result.duration_seconds, "duration_seconds": round(time.monotonic() - started, 3),
        "ocr_languages": ocr.languages if result.ocr_pages else None,
        "warnings": result.warnings[:10],
        "page_issues": {str(p.page): p.issues[:6] for p in result.pages if p.issues and p.page is not None}
        if len(result.pages) <= 300 else {},
    }


def _doc_summary(stats: dict, result) -> dict:
    return {k: stats[k] for k in ("extraction_method", "methods_used", "total_pages", "extracted_pages", "ocr_pages",
                                  "failed_pages", "character_count", "mean_confidence", "file_type") if k in stats} | {
        "pdf_metadata": {k: str(v)[:200] for k, v in list(result.metadata.items())[:12]}}


# ------------------------------------------------------------------------------------------------ recovery + metrics
def recover_stale_jobs(db: Session, settings: Settings) -> dict:
    """Find RUNNING jobs whose worker died (expired lease) and PENDING jobs that were never dispatched.
    Returns the ids that should be re-queued by the caller. Jobs out of attempts go to DEAD_LETTER."""
    now = _now()
    requeue: list = []
    dead = 0
    stale = db.scalars(select(IngestionJob).where(IngestionJob.status == TaskStatus.RUNNING,
                                                  IngestionJob.lease_expires_at.is_not(None),
                                                  IngestionJob.lease_expires_at < now)).all()
    for job in stale:
        _merge_meta(job, errors=[*(job.meta or {}).get("errors", [])[-19:],
                                 {"attempt": job.attempts, "stage": job.stage, "code": "WORKER_LOST",
                                  "message": "Worker stopped before finishing; lease expired", "retryable": True,
                                  "at": now.isoformat()}])
        if job.attempts >= job.max_attempts:
            job.status, job.finished_at, dead = TaskStatus.DEAD_LETTER, now, dead + 1
            job.last_error = "[WORKER_LOST] Worker stopped before finishing; attempts exhausted"
        else:
            job.status = TaskStatus.RETRYING
            requeue.append(job.id)
        job.lease_expires_at = None
    cutoff = now - timedelta(minutes=settings.ingestion_pending_requeue_minutes)
    orphaned = db.scalars(select(IngestionJob).where(
        IngestionJob.status.in_([TaskStatus.PENDING, TaskStatus.RETRYING]), IngestionJob.updated_at < cutoff,
        IngestionJob.attempts < IngestionJob.max_attempts)).all()
    requeue.extend(j.id for j in orphaned if j.id not in requeue)
    db.commit()
    return {"requeue": requeue, "dead_lettered": dead, "stale_found": len(stale)}


def compute_ingestion_metrics(db: Session, days: int = 30, limit: int = 2000) -> dict:
    since = _now() - timedelta(days=max(1, min(days, 365)))
    jobs = db.scalars(select(IngestionJob).where(IngestionJob.created_at >= since)
                      .order_by(IngestionJob.created_at.desc()).limit(max(1, min(limit, 5000)))).all()
    status_counts = Counter(j.status.value for j in jobs)
    ok = [j for j in jobs if j.status == TaskStatus.SUCCEEDED]
    stats = [(j.meta or {}).get("stats") or {} for j in ok]
    real = [s for s in stats if not s.get("duplicate")]
    engines: Counter[str] = Counter(s.get("extraction_method", "unknown") for s in real)
    ar_scores = [s["arabic_quality"]["mean_score"] for s in real
                 if (s.get("arabic_quality") or {}).get("mean_score") is not None]
    durations = [s["duration_seconds"] for s in real if "duration_seconds" in s]
    pages = sum(s.get("total_pages", 0) for s in real)
    extracted = sum(s.get("extracted_pages", 0) for s in real)
    finished = len([j for j in jobs if j.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.DEAD_LETTER)])
    return {
        "window_days": days, "jobs_considered": len(jobs), "status_counts": dict(status_counts),
        "documents_processed": len(real), "duplicates_skipped": sum(1 for s in stats if s.get("duplicate")),
        "pages_processed": pages, "pages_extracted": extracted, "pages_requiring_ocr": sum(s.get("ocr_pages", 0) for s in real),
        "extraction_page_success_rate": round(extracted / pages, 4) if pages else None,
        "job_success_rate": round(len(ok) / finished, 4) if finished else None,
        "arabic_extraction_quality_mean": round(sum(ar_scores) / len(ar_scores), 4) if ar_scores else None,
        "avg_processing_seconds": round(sum(durations) / len(durations), 3) if durations else None,
        "chunks_generated": sum(s.get("chunks_created", 0) for s in real),
        "failed_jobs": status_counts.get("FAILED", 0) + status_counts.get("DEAD_LETTER", 0),
        "retry_attempts_total": sum(max(0, j.attempts - 1) for j in jobs),
        "engine_usage": dict(engines),
    }
