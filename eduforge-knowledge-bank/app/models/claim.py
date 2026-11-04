from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, UUIDPkMixin, utcnow
from app.models.enums import ClaimStatus, ClaimType, EvidenceStance
from app.models.source import _enum


class ExtractedClaim(UUIDPkMixin, TimestampMixin, Base):
    """A claim extracted from a raw document. Never VERIFIED by default; verification is a separate workflow."""

    __tablename__ = "extracted_claims"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("evidence_score >= 0 AND evidence_score <= 1", name="evidence_score_range"),
        CheckConstraint("raw_document_id IS NOT NULL OR learning_asset_id IS NOT NULL", name="has_origin"),
        Index("ix_claims_domain_status", "domain_id", "status"),
    )

    # A claim originates either in a source document or in a generated learning asset (never both required).
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("raw_documents.id", ondelete="RESTRICT"), index=True)
    learning_asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("learning_assets.id", ondelete="RESTRICT"), index=True)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("knowledge_domains.id", ondelete="SET NULL"))
    topic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("topics.id", ondelete="SET NULL"))
    concept_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("concepts.id", ondelete="SET NULL"))
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[ClaimType] = mapped_column(_enum(ClaimType), default=ClaimType.OTHER, nullable=False)
    status: Mapped[ClaimStatus] = mapped_column(_enum(ClaimStatus), default=ClaimStatus.RAW, nullable=False)
    span_start: Mapped[int | None] = mapped_column(Integer)
    span_end: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    evidence_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    extraction_method: Mapped[str | None] = mapped_column(String(128))
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class EvidenceRecord(UUIDPkMixin, Base):
    """Evidence for/against a claim. `independence_group` collapses copies of the same origin into one vote."""

    __tablename__ = "evidence_records"
    __table_args__ = (
        CheckConstraint("authority_score >= 0 AND authority_score <= 1", name="authority_range"),
        CheckConstraint("relevance_score >= 0 AND relevance_score <= 1", name="relevance_range"),
    )

    claim_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("extracted_claims.id", ondelete="RESTRICT"), nullable=False, index=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("sources.id", ondelete="RESTRICT"), index=True)
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("raw_documents.id", ondelete="RESTRICT"))
    stance: Mapped[EvidenceStance] = mapped_column(_enum(EvidenceStance), nullable=False)
    independence_group: Mapped[str | None] = mapped_column(String(128), index=True)
    authority_score: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    methodology: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
