from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, SoftDeleteMixin, TimestampMixin, UUIDPkMixin, utcnow
from app.models.enums import DocumentStatus, SourcePolicy, SourceType

_ACTIVE = text("deleted_at IS NULL")


def _enum(e):  # VARCHAR-backed enum
    return Enum(e, native_enum=False, length=32, validate_strings=True)


class Source(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Registry entry for an external source. Only policy=ALLOWED sources may be fetched."""

    __tablename__ = "sources"
    __table_args__ = (
        Index("uq_sources_host_active", "host", unique=True, postgresql_where=_ACTIVE, sqlite_where=_ACTIVE),
        CheckConstraint("authority_score >= 0 AND authority_score <= 1", name="authority_range"),
        CheckConstraint("rate_limit_per_minute > 0", name="rate_limit_positive"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[SourceType] = mapped_column(_enum(SourceType), default=SourceType.WEBSITE, nullable=False)
    policy: Mapped[SourcePolicy] = mapped_column(_enum(SourcePolicy), default=SourcePolicy.PENDING, nullable=False, index=True)
    authority_score: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class SourceSnapshot(UUIDPkMixin, Base):
    """Immutable record of one fetch of one URL."""

    __tablename__ = "source_snapshots"
    __table_args__ = (UniqueConstraint("source_id", "url", "content_hash", name="uq_snapshot_source_url_hash"),)

    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(4096), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # sha256 hex
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    etag: Mapped[str | None] = mapped_column(String(255))
    storage_uri: Mapped[str | None] = mapped_column(String(2048))
    headers: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class RawDocument(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "raw_documents"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_raw_documents_content_hash"),)

    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("sources.id", ondelete="RESTRICT"), index=True)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("source_snapshots.id", ondelete="RESTRICT"))
    domain_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("knowledge_domains.id", ondelete="SET NULL"), index=True)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("topics.id", ondelete="SET NULL"))
    title: Mapped[str | None] = mapped_column(String(1000))
    original_url: Mapped[str | None] = mapped_column(String(4096))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str | None] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 of normalized text
    text_content: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DocumentStatus] = mapped_column(_enum(DocumentStatus), default=DocumentStatus.INGESTED, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
