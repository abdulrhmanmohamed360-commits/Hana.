from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.core.config import Settings, get_settings
from app.core.errors import InvalidInputError, NotFoundError
from app.models import IngestionJob, Source
from app.models.enums import SourcePolicy, TaskStatus
from app.services.ingestion import IngestionError, store_upload

router = APIRouter(prefix="/documents", tags=["documents"], dependencies=[Depends(enforce_rate_limit)])


@router.post("/ingest", status_code=202)
async def ingest_document(
    file: UploadFile = File(...),
    source_id: uuid.UUID | None = Form(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    who: Principal = Depends(require_role(Role.EDITOR)),
):
    if source_id:
        source = db.get(Source, source_id)
        if source is None:
            raise NotFoundError("Source not found")
        if source.policy != SourcePolicy.ALLOWED:
            raise InvalidInputError("Source must be explicitly ALLOWED before ingestion")
    try:
        data = await file.read(settings.max_upload_bytes + 1)
        path, file_digest = store_upload(data, file.filename or "upload.txt", settings.storage_dir,
                                         settings.max_upload_bytes)
    except IngestionError as exc:
        raise InvalidInputError(str(exc)) from exc
    except OSError as exc:
        raise InvalidInputError("Could not store uploaded document") from exc
    job = IngestionJob(
        source_id=source_id,
        input_uri=path,
        status=TaskStatus.PENDING,
        created_by=who.actor,
        meta={"original_name": Path(file.filename or "upload.txt").name, "file_sha256": file_digest},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        from app.tasks import process_ingestion
        process_ingestion.delay(str(job.id))
    except Exception as exc:
        job.status = TaskStatus.FAILED
        job.last_error = f"Queue unavailable: {exc}"[:4000]
        db.commit()
        raise InvalidInputError("Document saved but the ingestion queue is unavailable; retry after the worker is online")
    return {"job_id": str(job.id), "status": job.status.value, "message": "Document accepted for ingestion"}


@router.get("/jobs/{job_id}")
def get_ingestion_job(job_id: uuid.UUID, db: Session = Depends(get_db),
                      _: Principal = Depends(require_role(Role.READER))):
    job = db.get(IngestionJob, job_id)
    if job is None:
        raise NotFoundError("Ingestion job not found")
    return {"job_id": str(job.id), "status": job.status.value, "attempts": job.attempts,
            "documents_created": job.documents_created, "duplicates_skipped": job.duplicates_skipped,
            "last_error": job.last_error, "created_at": job.created_at, "finished_at": job.finished_at}
