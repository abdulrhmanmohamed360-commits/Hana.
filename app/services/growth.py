"""Knowledge growth planning and bounded external educational source discovery."""
from __future__ import annotations

import hashlib
from typing import Any

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.enums import TaskStatus
from app.models.learning import LearningAsset
from app.models.ops import ResearchTask
from app.models.source import RawDocument, Source, SourceSnapshot
from app.models.knowledge import KnowledgeChunk
from app.models.enums import DocumentStatus, SourcePolicy
from app.core.url_policy import UrlPolicyError, validate_url
from app.services.ingestion import content_hash, normalize_text
from app.services.chunking import TextSegment, chunk_segments

REQUIRED_ASSET_TYPES = ("explanation", "example", "exercise", "quiz", "summary", "glossary")


def find_knowledge_gaps(db: Session, limit: int = 200) -> list[dict[str, Any]]:
    """Find missing content types for curriculum/lesson combinations already in the bank."""
    rows = db.scalars(select(LearningAsset).where(LearningAsset.status == "APPROVED")
                      .order_by(LearningAsset.education_system, LearningAsset.stage,
                                LearningAsset.grade, LearningAsset.subject, LearningAsset.lesson)
                      .limit(10000)).all()
    groups: dict[tuple, set[str]] = {}
    for row in rows:
        key = (row.education_system, row.stage, row.grade, row.subject, row.unit, row.lesson, row.language)
        groups.setdefault(key, set()).add(row.asset_type)
    gaps = []
    for key, present in groups.items():
        missing = [kind for kind in REQUIRED_ASSET_TYPES if kind not in present]
        if missing:
            gaps.append({
                "education_system": key[0], "stage": key[1], "grade": key[2],
                "subject": key[3], "unit": key[4], "lesson": key[5],
                "language": key[6], "available_types": sorted(present),
                "missing_types": missing, "gap_count": len(missing),
            })
        if len(gaps) >= limit:
            break
    return gaps


def create_research_tasks(db: Session, actor: str, limit: int = 100) -> list[ResearchTask]:
    created = []
    for gap in find_knowledge_gaps(db, limit=limit):
        goal = (f"Find reliable educational sources for {gap['subject']} / {gap['grade']} / "
                f"{gap.get('lesson') or gap.get('unit') or 'curriculum'}; prepare: "
                f"{', '.join(gap['missing_types'])}")
        key_material = "|".join(str(gap.get(k) or "") for k in
                                 ("education_system", "stage", "grade", "subject", "unit", "lesson", "language"))
        key_material += "|missing=" + ",".join(sorted(gap["missing_types"]))
        idem = "growth:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:48]
        existing = db.scalar(select(ResearchTask).where(ResearchTask.idempotency_key == idem))
        if existing:
            continue
        task = ResearchTask(goal=goal, created_by=actor, idempotency_key=idem,
                            status=TaskStatus.PENDING, priority=4,
                            meta={"origin": "knowledge_growth_scan", "gap": gap,
                                  "publication_requires_review": True})
        db.add(task)
        created.append(task)
    db.commit()
    for task in created:
        db.refresh(task)
    return created


def search_educational_sources(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Search via Tavily when configured; results are leads, never verified facts."""
    settings = get_settings()
    if not settings.tavily_api_key or not settings.tavily_api_key.get_secret_value().strip():
        raise HTTPException(status_code=503, detail="External research is not configured. Set TAVILY_API_KEY.")
    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": settings.tavily_api_key.get_secret_value(), "query": query,
                  "search_depth": "advanced", "max_results": max_results,
                  "include_answer": False, "include_raw_content": False},
            timeout=settings.research_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        results = []
        for item in payload.get("results", []):
            if not isinstance(item, dict) or not item.get("url") or not item.get("title"):
                continue
            results.append({"title": str(item["title"])[:500], "url": str(item["url"])[:2048],
                            "snippet": str(item.get("content") or "")[:4000],
                            "score": item.get("score"), "verification_status": "UNVERIFIED_LEAD"})
        return results
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"External research failed: {type(exc).__name__}") from exc


def _fetch_registered_page(db: Session, url: str, title: str, task_id: str) -> dict[str, Any]:
    """Fetch and index one result only when its exact host is explicitly allowlisted.

    Redirects are deliberately disabled: every outbound destination must be independently
    registered and revalidated to avoid SSRF through redirect chains.
    """
    settings = get_settings()
    try:
        checked = validate_url(url, allow_private=settings.allow_private_networks, resolve=True)
    except UrlPolicyError as exc:
        return {"url": url, "ingest_status": "BLOCKED_URL_POLICY", "reason": str(exc)[:300]}

    source = db.scalar(select(Source).where(Source.host == checked.host, Source.deleted_at.is_(None)))
    if source is None or source.policy != SourcePolicy.ALLOWED:
        return {"url": url, "ingest_status": "NOT_ALLOWLISTED",
                "reason": "Register the host and set its policy to ALLOWED before fetching."}

    max_bytes = 3 * 1024 * 1024
    try:
        # Stream with a hard byte ceiling; do not buffer an unbounded remote response.
        with httpx.Client(timeout=settings.research_timeout_seconds, follow_redirects=False,
                          headers={"User-Agent": "EduForgeKnowledgeBot/1.0"}) as client:
            with client.stream("GET", url) as response:
                if 300 <= response.status_code < 400:
                    return {"url": url, "ingest_status": "REDIRECT_BLOCKED", "reason": "Redirects are not followed."}
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                body_parts = []
                total = 0
                for part in response.iter_bytes():
                    total += len(part)
                    if total > max_bytes:
                        return {"url": url, "ingest_status": "TOO_LARGE", "reason": f"Page exceeds {max_bytes} bytes."}
                    body_parts.append(part)
                body = b"".join(body_parts)
                response_status = response.status_code
                response_headers = dict(response.headers)
    except httpx.HTTPError as exc:
        return {"url": url, "ingest_status": "FETCH_FAILED", "reason": type(exc).__name__}

    if content_type not in ("text/html", "text/plain", "text/markdown", "application/xhtml+xml"):
        return {"url": url, "ingest_status": "UNSUPPORTED_CONTENT_TYPE", "content_type": content_type}

    raw = body.decode("utf-8", errors="replace")
    if content_type in ("text/html", "application/xhtml+xml"):
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
            node.decompose()
        extracted_title = soup.title.get_text(" ", strip=True) if soup.title else title
        text_content = normalize_text(soup.get_text("\n"))
    else:
        extracted_title, text_content = title, normalize_text(raw)
    if len(text_content) < 80:
        return {"url": url, "ingest_status": "INSUFFICIENT_TEXT", "character_count": len(text_content)}

    digest = content_hash(text_content)
    existing = db.scalar(select(RawDocument).where(RawDocument.content_hash == digest,
                                                   RawDocument.deleted_at.is_(None)))
    if existing:
        return {"url": url, "ingest_status": "DUPLICATE", "document_id": str(existing.id)}

    snapshot = SourceSnapshot(source_id=source.id, url=url, http_status=response_status,
                              content_type=content_type, content_hash=hashlib.sha256(body).hexdigest(),
                              size_bytes=len(body), etag=response_headers.get("etag"),
                              headers={"content_language": response_headers.get("content-language", "")})
    db.add(snapshot)
    db.flush()
    document = RawDocument(source_id=source.id, snapshot_id=snapshot.id, title=(extracted_title or title)[:1000],
                           original_url=url, mime_type=content_type, language=source.language or "und",
                           content_hash=digest, text_content=text_content, status=DocumentStatus.EXTRACTED,
                           meta={"collected_by": "knowledge_growth", "research_task_id": task_id,
                                 "verification_status": "UNVERIFIED_SOURCE_CONTENT"})
    db.add(document)
    db.flush()
    chunks = chunk_segments([TextSegment(text_content, None)])
    for chunk in chunks:
        db.add(KnowledgeChunk(document_id=document.id, domain_id=None, topic_id=None,
                              chunk_index=chunk.index, page_start=None, page_end=None,
                              content=chunk.content, content_hash=chunk.content_hash,
                              language=source.language or "und", token_estimate=chunk.token_estimate,
                              meta={"source_url": url, "verification_status": "UNVERIFIED"}))
    return {"url": url, "ingest_status": "INGESTED_UNVERIFIED", "document_id": str(document.id),
            "snapshot_id": str(snapshot.id), "chunks_created": len(chunks), "character_count": len(text_content)}


def execute_research_task(db: Session, task: ResearchTask, max_results: int = 5) -> dict:
    """Search, then fetch only explicitly allowlisted hosts; never auto-approve content."""
    task.status = TaskStatus.RUNNING
    task.attempts += 1
    db.commit()
    try:
        results = search_educational_sources(task.goal, max_results=max_results)
        fetch_results = []
        for item in results:
            fetch_results.append(_fetch_registered_page(db, item["url"], item["title"], str(task.id)))
        task.meta = {**(task.meta or {}), "search_results": results,
                     "fetch_results": fetch_results, "result_count": len(results),
                     "ingested_count": sum(1 for item in fetch_results if item["ingest_status"] == "INGESTED_UNVERIFIED"),
                     "results_are_unverified": True,
                     "next_step": "review_claims_and_verify_before_creating_approved_assets"}
        task.status = TaskStatus.SUCCEEDED
        task.last_error = None
        db.commit()
        db.refresh(task)
        return {"task_id": str(task.id), "status": task.status.value,
                "results": results, "fetch_results": fetch_results,
                "result_count": len(results), "publication_allowed": False}
    except Exception as exc:
        db.rollback()
        task = db.get(ResearchTask, task.id)
        if task is not None:
            task.status = TaskStatus.FAILED
            task.last_error = type(exc).__name__
            db.commit()
        raise
