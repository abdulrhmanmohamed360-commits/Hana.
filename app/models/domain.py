from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, SoftDeleteMixin, TimestampMixin, UUIDPkMixin

_ACTIVE = text("deleted_at IS NULL")


class KnowledgeDomain(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "knowledge_domains"
    __table_args__ = (
        Index("uq_knowledge_domains_slug_active", "slug", unique=True, postgresql_where=_ACTIVE, sqlite_where=_ACTIVE),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    default_language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    education_level: Mapped[str | None] = mapped_column(String(64))  # school / university / professional ...
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    topics: Mapped[list["Topic"]] = relationship(back_populates="domain")


class Topic(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Hierarchical topic tree. `path` is the slug path ("algebra/linear-equations"), unique per domain."""

    __tablename__ = "topics"
    __table_args__ = (
        Index("uq_topics_domain_path_active", "domain_id", "path", unique=True, postgresql_where=_ACTIVE, sqlite_where=_ACTIVE),
        Index("ix_topics_domain_parent", "domain_id", "parent_id"),
    )

    domain_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("knowledge_domains.id", ondelete="RESTRICT"), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("topics.id", ondelete="RESTRICT"))
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    domain: Mapped[KnowledgeDomain] = relationship(back_populates="topics")


class Concept(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "concepts"
    __table_args__ = (
        Index("uq_concepts_domain_slug_active", "domain_id", "slug", unique=True, postgresql_where=_ACTIVE, sqlite_where=_ACTIVE),
    )

    domain_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("knowledge_domains.id", ondelete="RESTRICT"), nullable=False, index=True)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("topics.id", ondelete="SET NULL"), index=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(300), nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
