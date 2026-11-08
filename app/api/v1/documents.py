"""Document upload and ingestion API."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    Principal,
    Role,
    enforce_rate_limit,
    get_db,
    require_role,
)
from app.core.config import Settings, get_settings
from app.core.errors import InvalidInputError, NotFoundError
from app.models import IngestionJob, Source
from app.models.enums import SourcePolicy, TaskStatus
from app.services.docreader import SUPPORTED_EXTENSIONS, safe_filename
from app.services.ingestion import IngestionError, store_upload
from app.services.storage import (
    StorageError,
    create_signed_upload,
    object_exists,
)

router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(enforce_rate_limit)],
)


class SignedUploadRequest(BaseModel):
    original_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(
        default="application/octet-stream",
        max_length=150,
    )
    file_size: int = Field(gt=0)


class CompleteUploadRequest(BaseModel):
    object_path: str = Field(min_length=1, max_length=1024)
    original_name: str = Field(min_length=1, max_length=255)
    source_id: uuid.UUID | None = None
    file_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-fA-F0-9]{64}$",
    )


def _validate_source(
    db: Session,
    source_id: uuid.UUID | None,
) -> None:
    if source_id is None:
        return

    source = db.get(Source, source_id)

    if source is None:
        raise NotFoundError("Source not found")

    if source.policy != SourcePolicy.ALLOWED:
        raise InvalidInputError(
            "Source must be explicitly ALLOWED before ingestion"
        )


def _queue_ingestion(
    db: Session,
    job: IngestionJob,
) -> None:
    try:
        from app.tasks import process_ingestion

        process_ingestion.delay(str(job.id))

    except Exception as exc:
        job.status = TaskStatus.FAILED
        job.last_error = f"Queue unavailable: {exc}"[:4000]
        db.commit()

        raise InvalidInputError(
            "Document uploaded but the ingestion queue is unavailable"
        ) from exc


@router.post("/uploads/sign", status_code=200)
def create_upload_url(
    payload: SignedUploadRequest,
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require_role(Role.EDITOR)),
):
    """Create a temporary signed URL for direct browser-to-Supabase upload."""

    if payload.file_size > settings.max_upload_bytes:
        raise InvalidInputError(
            f"File exceeds the {settings.max_upload_bytes}-byte upload limit"
        )

    try:
        object_path, upload_url = create_signed_upload(
            settings=settings,
            original_name=payload.original_name,
            content_type=payload.content_type,
        )

    except StorageError as exc:
        raise InvalidInputError(str(exc)) from exc

    return {
        "object_path": object_path,
        "upload_url": upload_url,
        "upload_method": "PUT",
        "content_type": payload.content_type,
        "message": "Upload URL created. Upload the file directly to Supabase.",
    }


@router.post("/uploads/complete", status_code=202)
def complete_upload(
    payload: CompleteUploadRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    who: Principal = Depends(require_role(Role.EDITOR)),
):
    """Verify the uploaded object and enqueue document ingestion."""

    _validate_source(db, payload.source_id)

    if not payload.object_path.startswith("incoming/"):
        raise InvalidInputError("Invalid storage object path")

    if Path(safe_filename(payload.original_name)).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise InvalidInputError("Unsupported file type")

    try:
        exists = object_exists(
            settings=settings,
            object_path=payload.object_path,
        )

    except StorageError as exc:
        raise InvalidInputError(str(exc)) from exc

    if not exists:
        raise InvalidInputError(
            "Uploaded document was not found in Supabase Storage"
        )

    bucket = settings.supabase_storage_bucket
    storage_uri = f"supabase://{bucket}/{payload.object_path}"

    job = IngestionJob(
        source_id=payload.source_id,
        input_uri=storage_uri,
        status=TaskStatus.PENDING,
        created_by=who.actor,
        meta={
            "original_name": safe_filename(payload.original_name),
            "storage_backend": "supabase",
            "storage_bucket": bucket,
            "storage_object_path": payload.object_path,
            "file_sha256": payload.file_sha256,
        },
    )

    db.add(job)
    db.commit()
    db.refresh(job)

    _queue_ingestion(db, job)

    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "message": "Document accepted for ingestion",
    }


# Legacy upload endpoint retained for backward compatibility.
# Direct Supabase upload should be used for large files on Vercel.

@router.post("/ingest", status_code=202)
async def ingest_document(
    file: UploadFile = File(...),
    source_id: uuid.UUID | None = Form(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    who: Principal = Depends(require_role(Role.EDITOR)),
):
    _validate_source(db, source_id)

    try:
        data = await file.read(settings.max_upload_bytes + 1)

        path, file_digest = store_upload(
            data,
            file.filename or "upload.txt",
            settings.storage_dir,
            settings.max_upload_bytes,
        )

    except IngestionError as exc:
        raise InvalidInputError(str(exc)) from exc

    except OSError as exc:
        raise InvalidInputError(
            "Could not store uploaded document"
        ) from exc

    finally:
        await file.close()

    job = IngestionJob(
        source_id=source_id,
        input_uri=path,
        status=TaskStatus.PENDING,
        created_by=who.actor,
        meta={
            "original_name": safe_filename(file.filename or "upload.txt"),
            "file_sha256": file_digest,
            "storage_backend": "local",
        },
    )

    db.add(job)
    db.commit()
    db.refresh(job)

    _queue_ingestion(db, job)

    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "message": "Document accepted for ingestion",
    }


def _job_payload(job: IngestionJob) -> dict:
    """Public job view. Never includes storage URIs, signed URLs, tokens or other internal secrets."""
    meta = job.meta or {}
    stats = meta.get("stats") or {}
    errors = [
        {
            "attempt": e.get("attempt"),
            "stage": e.get("stage"),
            "code": e.get("code"),
            "message": e.get("message"),
            "retryable": e.get("retryable"),
            "at": e.get("at"),
        }
        for e in (meta.get("errors") or [])
        if isinstance(e, dict)
    ]
    return {
        # Original fields (unchanged contract)
        "job_id": str(job.id),
        "status": job.status.value,
        "attempts": job.attempts,
        "documents_created": job.documents_created,
        "duplicates_skipped": job.duplicates_skipped,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        # Additive fields
        "stage": job.stage,
        "max_attempts": job.max_attempts,
        "document_id": meta.get("document_id"),
        "file_type": stats.get("file_type"),
        "extraction_method": stats.get("extraction_method"),
        "methods_used": stats.get("methods_used"),
        "total_pages": stats.get("total_pages"),
        "extracted_pages": stats.get("extracted_pages"),
        "ocr_pages": stats.get("ocr_pages"),
        "extracted_characters": stats.get("character_count"),
        "chunks_created": stats.get("chunks_created"),
        "failed_pages": stats.get("failed_pages", []),
        "mean_confidence": stats.get("mean_confidence"),
        "language": stats.get("language"),
        "duplicate": bool(stats.get("duplicate", False)),
        "embedding": stats.get("embedding"),
        "warnings": stats.get("warnings", []),
        "error_code": meta.get("last_error_code"),
        "errors": errors,
    }


@router.get("/jobs/{job_id}")
def get_ingestion_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_role(Role.READER)),
):
    job = db.get(IngestionJob, job_id)

    if job is None:
        raise NotFoundError("Ingestion job not found")

    return _job_payload(job)


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_ingestion_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    who: Principal = Depends(require_role(Role.EDITOR)),
):
    """Recover a FAILED or DEAD_LETTER job: grants a fresh bounded set of attempts and re-queues it."""
    job = db.get(IngestionJob, job_id)

    if job is None:
        raise NotFoundError("Ingestion job not found")

    if job.status not in (TaskStatus.FAILED, TaskStatus.DEAD_LETTER):
        raise InvalidInputError(
            f"Only FAILED or DEAD_LETTER jobs can be retried (current status: {job.status.value})"
        )

    job.max_attempts = job.attempts + settings.ingestion_max_retries + 1
    job.status = TaskStatus.PENDING
    job.stage = None
    job.finished_at = None
    job.lease_expires_at = None
    job.meta = {**(job.meta or {}), "manual_retries": int((job.meta or {}).get("manual_retries", 0)) + 1,
                "manual_retry_by": who.actor}
    db.commit()
    db.refresh(job)

    _queue_ingestion(db, job)

    return {"job_id": str(job.id), "status": job.status.value, "message": "Job re-queued"}


@router.get("/ingestion/metrics")
def ingestion_metrics(
    days: int = 30,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_role(Role.READER)),
):
    """Aggregate extraction quality and throughput metrics over recent ingestion jobs."""
    from app.services.ingestion_pipeline import compute_ingestion_metrics

    return compute_ingestion_metrics(db, days=days)
