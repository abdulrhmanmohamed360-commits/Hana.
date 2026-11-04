from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (Principal, Role, enforce_rate_limit, get_db, get_embedder, get_job_dispatcher,
                          require_role)
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.engines.text_ar import normalize_arabic
from app.models import PreparationJob
from app.models.enums import TaskStatus
from app.schemas.prepare import JobAccepted, JobStatus, PreparePackage, PrepareRequest
from app.services.embeddings import EmbeddingError, embed_pending_chunks
from app.services.preparation_jobs import create_or_get_job
from app.services.prepare import PrepareContext, build_package
from app.services.retrieval import hit_to_dict, retrieve_hybrid

log = get_logger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"], dependencies=[Depends(enforce_rate_limit)])


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    domain_id: uuid.UUID | None = None
    limit: int = Field(default=8, ge=1, le=20)


@router.post("/retrieve")
def retrieve_knowledge(payload: RetrieveRequest, db: Session = Depends(get_db), embedder=Depends(get_embedder),
                       _: Principal = Depends(require_role(Role.READER))):
    """Hybrid retrieval (lexical + PostgreSQL full-text + pgvector, fused by RRF). Returns passages with provenance;
    it never generates an answer. `signals_unavailable` states honestly which signals could not run."""
    vec = model = None
    if embedder is not None:
        try:
            vec, model = embedder.embed([normalize_arabic(payload.query)])[0], embedder.model
        except EmbeddingError as exc:
            log.warning("query embedding failed: %s", exc)
    res = retrieve_hybrid(db, payload.query, limit=payload.limit, domain_id=payload.domain_id,
                          query_embedding=vec, embedding_model=model)
    return {"query": payload.query, "count": len(res.hits), "results": [hit_to_dict(h) for h in res.hits],
            "retrieval_mode": res.mode, "signals_used": res.signals_used,
            "signals_unavailable": res.signals_unavailable, "answer_generation": "not_enabled"}


@router.post("/prepare", response_model=PreparePackage | JobAccepted, status_code=200,
             responses={202: {"model": JobAccepted, "description": "mode=async: job queued; poll status_url"}})
def prepare_knowledge(body: PrepareRequest, response: Response, db: Session = Depends(get_db),
                      embedder=Depends(get_embedder), dispatch=Depends(get_job_dispatcher),
                      who: Principal = Depends(require_role(Role.EDITOR))):
    """Prepare a structured lesson package.

    * mode=sync  — retrieval + deterministic generation + verification only: no LLM calls, no external research
      (at most one query-embedding call when an embedding provider is configured).
    * mode=async — also runs the LLM agents and optional external research; returns 202 and a job to poll.
    Generated content is stored as DRAFT and is never returned as approved.
    """
    if body.mode == "async":
        job, duplicate = create_or_get_job(db, body, who.actor)
        if not duplicate:
            try:
                dispatch(str(job.id))
            except Exception as exc:  # broker down: the job is persisted and the beat safety net will pick it up
                log.warning("could not enqueue preparation job %s: %s", job.id, type(exc).__name__)
        response.status_code = 202
        return JobAccepted(job_id=job.id, status=job.status.value,
                           status_url=f"/api/v1/knowledge/jobs/{job.id}", deduplicated=duplicate)
    return build_package(db, body, PrepareContext(actor=who.actor, embedder=embedder, allow_llm=False))


def _job_status(job: PreparationJob) -> JobStatus:
    return JobStatus(job_id=job.id, status=job.status.value, progress=job.progress or {}, attempts=job.attempts,
                     max_attempts=job.max_attempts, last_error=job.last_error, created_at=job.created_at,
                     started_at=job.started_at, finished_at=job.finished_at,
                     result=PreparePackage.model_validate(job.result) if job.result else None)


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_preparation_job(job_id: uuid.UUID, db: Session = Depends(get_db),
                        _: Principal = Depends(require_role(Role.EDITOR))):
    job = db.get(PreparationJob, job_id)
    if job is None:
        raise NotFoundError("Preparation job not found")
    return _job_status(job)


@router.get("/jobs")
def list_preparation_jobs(status: str | None = None, limit: int = Query(default=20, ge=1, le=100),
                          db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.EDITOR))):
    stmt = select(PreparationJob).order_by(PreparationJob.created_at.desc()).limit(limit)
    if status:
        try:
            stmt = stmt.where(PreparationJob.status == TaskStatus(status))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid job status") from exc
    return [{"job_id": str(j.id), "status": j.status.value, "progress": j.progress, "attempts": j.attempts,
             "lesson": (j.request or {}).get("lesson"), "created_at": j.created_at, "finished_at": j.finished_at}
            for j in db.scalars(stmt)]


@router.post("/embeddings/backfill")
def backfill_embeddings(batch: int = Query(default=64, ge=1, le=256), db: Session = Depends(get_db),
                        embedder=Depends(get_embedder), _: Principal = Depends(require_role(Role.EDITOR))):
    if embedder is None:
        raise HTTPException(status_code=503, detail="Embeddings are not configured. Set EMBEDDING_API_KEY or LLM_API_KEY.")
    try:
        return {"embedded": embed_pending_chunks(db, embedder, batch), "model": embedder.model}
    except EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
