from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    event, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, EmbeddingType, JSONType, SoftDeleteMixin, TimestampMixin, UUIDPkMixin, utcnow
from app.models.enums import KnowledgeItemType, KnowledgeStatus, RelationType
from app.models.source import _enum
from app.engines.text_ar import normalize_arabic


class KnowledgeItem(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Stable identity of a piece of knowledge; its content lives in immutable KnowledgeVersion rows."""

    __tablename__ = "knowledge_items"
    __table_args__ = (Index("ix_knowledge_items_domain_status", "domain_id", "status"),)

    domain_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("knowledge_domains.id", ondelete="RESTRICT"), nullable=False)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("topics.id", ondelete="SET NULL"))
    concept_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("concepts.id", ondelete="SET NULL"))
    item_type: Mapped[KnowledgeItemType] = mapped_column(_enum(KnowledgeItemType), nullable=False)
    status: Mapped[KnowledgeStatus] = mapped_column(_enum(KnowledgeStatus), default=KnowledgeStatus.DRAFT, nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)


class KnowledgeVersion(UUIDPkMixin, Base):
    """Append-only. A change creates a new version row; old rows are never overwritten."""

    __tablename__ = "knowledge_versions"
    __table_args__ = (
        UniqueConstraint("knowledge_item_id", "version_number", name="uq_version_item_number"),
        Index("uq_knowledge_versions_one_current", "knowledge_item_id", unique=True,
              postgresql_where=text("is_current"), sqlite_where=text("is_current = 1")),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("version_number >= 1", name="version_positive"),
    )

    knowledge_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("knowledge_items.id", ondelete="RESTRICT"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    evidence_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[KnowledgeStatus] = mapped_column(_enum(KnowledgeStatus), default=KnowledgeStatus.DRAFT, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("knowledge_versions.id", ondelete="RESTRICT"))
    source_claim_ids: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)  # provenance trail
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeRelationship(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_relationships"
    __table_args__ = (
        UniqueConstraint("source_concept_id", "target_concept_id", "relation_type", name="uq_relationship_triple"),
        CheckConstraint("source_concept_id <> target_concept_id", name="no_self_relation"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    source_concept_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False, index=True)
    target_concept_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False, index=True)
    relation_type: Mapped[RelationType] = mapped_column(_enum(RelationType), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="EXTRACTED", nullable=False)
    evidence_claim_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("extracted_claims.id", ondelete="SET NULL"))


class Embedding(UUIDPkMixin, Base):
    """Vector for any owner row (owner_type + owner_id). ANN index is added in Milestone 6."""

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "model", name="uq_embedding_owner_model"),
        Index("ix_embeddings_owner", "owner_type", "owner_id"),
    )

    owner_type: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding = mapped_column(EmbeddingType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeChunk(UUIDPkMixin, TimestampMixin, Base):
    """Searchable, provenance-preserving passage extracted from a raw document."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_document_index"),
        Index("ix_knowledge_chunks_document_page", "document_id", "page_start", "page_end"),
        Index("ix_knowledge_chunks_domain_topic", "domain_id", "topic_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("raw_documents.id", ondelete="CASCADE"), nullable=False)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("knowledge_domains.id", ondelete="SET NULL"))
    topic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("topics.id", ondelete="SET NULL"))
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(16), default="und", nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Arabic-normalized, case-folded copy of `content`; basis of lexical + full-text search.
    content_norm: Mapped[str | None] = mapped_column(Text)
    # Document-intelligence provenance (migration 0007). All nullable: legacy chunks simply lack them.
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("sources.id", ondelete="SET NULL"))
    section_title: Mapped[str | None] = mapped_column(String(500))
    chapter_title: Mapped[str | None] = mapped_column(String(500))
    extraction_method: Mapped[str | None] = mapped_column(String(32))
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


@event.listens_for(KnowledgeChunk, "before_insert")
def _fill_content_norm(mapper, connection, target: KnowledgeChunk) -> None:  # noqa: ARG001
    """Every insertion path (API worker, research fetch, tests) gets a normalized search copy."""
    if not target.content_norm and target.content:
        target.content_norm = normalize_arabic(target.content).casefold()
