"""Shared test helpers. The scripted LLM transport is a TEST DOUBLE for the network call only: the schemas,
retry logic, verification, persistence and API under test are the real code."""
from __future__ import annotations

import json
from typing import Callable

from app.models import KnowledgeChunk, RawDocument, Source
from app.models.enums import DocumentStatus, SourcePolicy
from app.services.ingestion import content_hash


def make_document(db, title: str, text: str, *, page: int = 1, source: Source | None = None,
                  doc_meta: dict | None = None, chunk_meta: dict | None = None, url: str | None = None):
    doc = RawDocument(title=title, content_hash=content_hash(text), text_content=text, status=DocumentStatus.EXTRACTED,
                      source_id=source.id if source else None, original_url=url, meta=doc_meta or {})
    db.add(doc)
    db.flush()
    chunk = KnowledgeChunk(document_id=doc.id, chunk_index=0, page_start=page, page_end=page, content=text,
                           content_hash=content_hash(text), language="ar", token_estimate=len(text.split()),
                           meta=chunk_meta or {})
    db.add(chunk)
    db.commit()
    return doc, chunk


def make_source(db, host: str, policy: SourcePolicy, authority: float = 0.8) -> Source:
    src = Source(name=host, base_url=f"https://{host}", host=host, policy=policy, authority_score=authority)
    db.add(src)
    db.commit()
    return src


def scripted_transport(replies: dict[str, list[str] | str]) -> tuple[Callable, list[dict]]:
    """Return (transport, calls). `replies` maps a role name found in the system prompt to a JSON string (or a list
    of strings consumed one per call, to script an invalid reply followed by a valid one)."""
    calls: list[dict] = []
    queues = {k: (list(v) if isinstance(v, list) else [v]) for k, v in replies.items()}

    def transport(messages: list[dict]) -> tuple[str, dict]:
        system = messages[0]["content"]
        role = next(r for r in queues if r in system)
        calls.append({"role": role, "turns": len(messages)})
        q = queues[role]
        return (q.pop(0) if len(q) > 1 else q[0]), {"prompt_tokens": 10, "completion_tokens": 5}

    return transport, calls


def teacher_json(**overrides) -> str:
    base = {"explanation": None, "definitions": [], "formulas": [], "summary": None, "evidence_ids_used": [],
            "solved_examples": [], "exercises": []}
    return json.dumps({**base, **overrides}, ensure_ascii=False)


RESEARCH_OK = json.dumps({"summary": "ملخص الأدلة", "key_points": [], "missing_content": [], "search_queries": []},
                         ensure_ascii=False)
ANALYSIS_OK = json.dumps({"prerequisites": ["العمليات الحسابية"], "concepts": [], "misconceptions": [],
                          "teaching_sequence": []}, ensure_ascii=False)
REVIEW_OK = json.dumps({"overall": "issues_found", "issues": [
    {"claim": "الكتابة بالقلم الرصاص", "problem": "لا علاقة له بالدرس", "severity": "medium"}]}, ensure_ascii=False)
