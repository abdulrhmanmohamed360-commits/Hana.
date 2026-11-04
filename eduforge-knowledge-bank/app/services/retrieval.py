"""Baseline explainable lexical retrieval over persisted knowledge chunks."""
from __future__ import annotations

import re
from collections import Counter
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KnowledgeChunk, RawDocument

_TOKEN = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)


def _terms(value: str) -> list[str]:
    return [t.casefold() for t in _TOKEN.findall(value) if len(t) > 1]


def retrieve_chunks(db: Session, query: str, limit: int = 8, domain_id: UUID | None = None) -> list[dict]:
    query_terms = _terms(query)
    if not query_terms:
        return []
    stmt = select(KnowledgeChunk, RawDocument).join(RawDocument, RawDocument.id == KnowledgeChunk.document_id)
    stmt = stmt.where(RawDocument.deleted_at.is_(None))
    if domain_id:
        stmt = stmt.where(KnowledgeChunk.domain_id == domain_id)
    rows = db.execute(stmt.limit(5000)).all()
    query_counts = Counter(query_terms)
    ranked: list[tuple[float, KnowledgeChunk, RawDocument]] = []
    for chunk, document in rows:
        content_terms = Counter(_terms(chunk.content))
        overlap = sum(min(count, content_terms.get(term, 0)) for term, count in query_counts.items())
        if not overlap:
            continue
        coverage = overlap / max(1, sum(query_counts.values()))
        density = overlap / max(1, len(_terms(chunk.content)) ** 0.5)
        phrase_bonus = 0.35 if query.casefold() in chunk.content.casefold() else 0.0
        score = round(coverage * 0.7 + density * 0.08 + phrase_bonus, 6)
        ranked.append((score, chunk, document))
    ranked.sort(key=lambda row: (row[0], row[1].created_at), reverse=True)
    results = []
    for score, chunk, document in ranked[:max(1, min(limit, 20))]:
        results.append({
            "chunk_id": str(chunk.id), "document_id": str(document.id),
            "title": document.title or "Untitled source", "source_url": document.original_url,
            "page_start": chunk.page_start, "page_end": chunk.page_end,
            "content": chunk.content, "score": score,
            "content_hash": chunk.content_hash,
        })
    return results



# ======================================================================================================
# Hybrid retrieval: lexical + PostgreSQL full-text + pgvector semantic search, fused with reciprocal rank
# fusion (RRF). On SQLite (unit tests) the FTS signal is unavailable and vectors are compared in Python.
# ======================================================================================================
import re
from dataclasses import dataclass, field

from sqlalchemy import func, literal_column, or_

from app.engines.fusion import cosine, reciprocal_rank_fusion
from app.engines.text_ar import normalize_arabic, tokenize
from app.models import Embedding

_SAFE_TERM = re.compile(r"^\w+$", re.UNICODE)
_STOP_TERMS = {"the", "and", "for", "with", "that", "this", "من", "في", "على", "الى", "عن", "هذا", "هذه"}
CANDIDATE_LIMIT = 2000
SIGNAL_LIMIT = 50
OWNER_CHUNK = "knowledge_chunk"


@dataclass
class HybridHit:
    chunk: KnowledgeChunk
    document: RawDocument
    score: float
    signals: dict[str, int] = field(default_factory=dict)  # signal -> 1-based rank


@dataclass
class HybridResult:
    hits: list[HybridHit]
    signals_used: list[str]
    signals_unavailable: dict[str, str]

    @property
    def mode(self) -> str:
        return "hybrid_rrf" if len(self.signals_used) > 1 else "lexical_baseline"


def _query_terms(query: str) -> list[str]:
    seen, out = set(), []
    for t in tokenize(query):
        if t not in _STOP_TERMS and t not in seen and _SAFE_TERM.match(t):
            seen.add(t)
            out.append(t)
    return out[:24]


def _lexical_ranking(db: Session, terms: list[str], query_norm: str, domain_id: UUID | None) -> list[UUID]:
    text_col = func.coalesce(KnowledgeChunk.content_norm, KnowledgeChunk.content)
    stmt = (select(KnowledgeChunk.id, text_col).join(RawDocument, RawDocument.id == KnowledgeChunk.document_id)
            .where(RawDocument.deleted_at.is_(None),
                   or_(*[text_col.like(f"%{t}%") for t in terms])).limit(CANDIDATE_LIMIT))
    if domain_id:
        stmt = stmt.where(KnowledgeChunk.domain_id == domain_id)
    counts = Counter(terms)
    scored: list[tuple[float, UUID]] = []
    for chunk_id, text in db.execute(stmt).all():
        words = Counter(tokenize(text))
        overlap = sum(1 for t in counts if words.get(t))
        if not overlap:
            continue
        coverage = overlap / len(counts)
        density = overlap / max(1, sum(words.values()) ** 0.5)
        phrase = 0.35 if query_norm and query_norm in text else 0.0
        scored.append((coverage * 0.7 + density * 0.08 + phrase, chunk_id))
    scored.sort(key=lambda t: -t[0])
    return [cid for _, cid in scored[:SIGNAL_LIMIT]]


def _fts_ranking(db: Session, terms: list[str], domain_id: UUID | None) -> list[UUID]:
    vec = func.to_tsvector(literal_column("'simple'"), func.coalesce(KnowledgeChunk.content_norm, ""))
    tsq = func.to_tsquery(literal_column("'simple'"), " | ".join(terms))
    rank = func.ts_rank_cd(vec, tsq)
    stmt = (select(KnowledgeChunk.id, rank).join(RawDocument, RawDocument.id == KnowledgeChunk.document_id)
            .where(RawDocument.deleted_at.is_(None), vec.op("@@")(tsq)).order_by(rank.desc()).limit(SIGNAL_LIMIT))
    if domain_id:
        stmt = stmt.where(KnowledgeChunk.domain_id == domain_id)
    return [cid for cid, _ in db.execute(stmt).all()]


def _vector_ranking(db: Session, query_embedding: list[float], model: str, domain_id: UUID | None) -> list[UUID]:
    if db.get_bind().dialect.name == "postgresql":
        dist = Embedding.embedding.cosine_distance(query_embedding)
        stmt = (select(Embedding.owner_id, dist).where(Embedding.owner_type == OWNER_CHUNK, Embedding.model == model)
                .order_by(dist).limit(SIGNAL_LIMIT))
        if domain_id:
            stmt = stmt.join(KnowledgeChunk, KnowledgeChunk.id == Embedding.owner_id).where(
                KnowledgeChunk.domain_id == domain_id)
        return [oid for oid, _ in db.execute(stmt).all()]
    rows = db.execute(select(Embedding.owner_id, Embedding.embedding).where(
        Embedding.owner_type == OWNER_CHUNK, Embedding.model == model).limit(5000)).all()
    scored = sorted(((cosine(query_embedding, list(vec)), oid) for oid, vec in rows if vec), key=lambda t: -t[0])
    return [oid for _, oid in scored[:SIGNAL_LIMIT]]


def retrieve_hybrid(db: Session, query: str, *, limit: int = 8, domain_id: UUID | None = None,
                    query_embedding: list[float] | None = None, embedding_model: str | None = None,
                    weights: dict[str, float] | None = None) -> HybridResult:
    """Fuse lexical, full-text and semantic rankings. Missing signals are reported, never faked."""
    limit = max(1, min(limit, 50))
    terms = _query_terms(query)
    rankings: dict[str, list[UUID]] = {}
    unavailable: dict[str, str] = {}
    if terms:
        rankings["lexical"] = _lexical_ranking(db, terms, normalize_arabic(query).casefold().strip(), domain_id)
        if db.get_bind().dialect.name == "postgresql":
            rankings["fts"] = _fts_ranking(db, terms, domain_id)
        else:
            unavailable["fts"] = "PostgreSQL full-text search is unavailable on this database dialect"
    else:
        unavailable["lexical"] = "query has no searchable terms"
    if query_embedding is not None and embedding_model:
        rankings["vector"] = _vector_ranking(db, query_embedding, embedding_model, domain_id)
    else:
        unavailable["vector"] = "no query embedding (embedding provider not configured or failed)"
    used = [name for name, ids in rankings.items() if ids]
    if not used:
        return HybridResult([], [], unavailable)
    fused = reciprocal_rank_fusion(rankings, weights={"fts": 1.0, "lexical": 0.8, "vector": 1.0, **(weights or {})})[:limit]
    ids = [item for item, _, _ in fused]
    rows = {c.id: (c, d) for c, d in db.execute(
        select(KnowledgeChunk, RawDocument).join(RawDocument, RawDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeChunk.id.in_(ids))).all()}
    hits = [HybridHit(rows[i][0], rows[i][1], score, ranks) for i, score, ranks in fused if i in rows]
    return HybridResult(hits, used, unavailable)


def hit_to_dict(hit: HybridHit) -> dict:
    c, d = hit.chunk, hit.document
    return {"chunk_id": str(c.id), "document_id": str(d.id), "title": d.title or "Untitled source",
            "source_url": d.original_url, "page_start": c.page_start, "page_end": c.page_end,
            "content": c.content, "score": round(hit.score, 6), "content_hash": c.content_hash,
            "signals": hit.signals,
            "verification_status": (c.meta or {}).get("verification_status", "SOURCE_MATERIAL")}
