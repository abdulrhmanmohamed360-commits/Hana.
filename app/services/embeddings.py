"""Embedding provider (OpenAI-compatible /embeddings) and chunk embedding backfill."""
from __future__ import annotations

from typing import Callable, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.base import EMBEDDING_DIMENSIONS
from app.models import Embedding, KnowledgeChunk

OWNER_CHUNK = "knowledge_chunk"


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatibleEmbedder:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int,
                 post: Callable[..., httpx.Response] | None = None):
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout
        self._post = post or httpx.post

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = self._post(f"{self.base_url}/embeddings", timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.api_key}"},
                              json={"model": self.model, "input": texts})
            resp.raise_for_status()
            rows = sorted(resp.json()["data"], key=lambda r: r["index"])
            vectors = [[float(x) for x in r["embedding"]] for r in rows]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"embedding request failed: {type(exc).__name__}") from exc
        if len(vectors) != len(texts) or any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            raise EmbeddingError(f"provider must return {EMBEDDING_DIMENSIONS}-dimensional vectors "
                                 f"(schema is fixed); got {sorted({len(v) for v in vectors})}")
        return vectors


def build_embedder(settings: Settings | None = None) -> OpenAICompatibleEmbedder | None:
    """None when no key is configured: hybrid retrieval then runs without the semantic signal and says so."""
    s = settings or get_settings()
    key = s.embedding_api_key or s.llm_api_key
    if key is None or not key.get_secret_value().strip():
        return None
    return OpenAICompatibleEmbedder(s.embedding_base_url or s.llm_base_url, key.get_secret_value(),
                                    s.embedding_model, s.embedding_timeout_seconds)


def embed_pending_chunks(db: Session, embedder: Embedder, batch_size: int = 64) -> int:
    """Embed up to `batch_size` chunks that lack a vector for this model. Returns number embedded."""
    done = select(Embedding.owner_id).where(Embedding.owner_type == OWNER_CHUNK, Embedding.model == embedder.model)
    chunks = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.id.not_in(done))
                        .order_by(KnowledgeChunk.created_at).limit(max(1, min(batch_size, 256)))).all()
    if not chunks:
        return 0
    vectors = embedder.embed([(c.content_norm or c.content)[:8000] for c in chunks])
    for chunk, vec in zip(chunks, vectors):
        db.add(Embedding(owner_type=OWNER_CHUNK, owner_id=chunk.id, model=embedder.model,
                         dimensions=len(vec), embedding=vec))
    db.commit()
    return len(chunks)
