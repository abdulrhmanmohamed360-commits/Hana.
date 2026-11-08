from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, UUIDPkMixin, utcnow


class LearningAsset(UUIDPkMixin, TimestampMixin, Base):
    """Reusable, curriculum-scoped educational content owned by the knowledge bank."""
    __tablename__ = "learning_assets"
    __table_args__ = (
        Index("ix_learning_assets_curriculum", "education_system", "stage", "grade", "subject", "unit", "lesson"),
        Index("ix_learning_assets_status_type", "status", "asset_type"),
    )

    education_system: Mapped[str] = mapped_column(String(100), default="general", nullable=False)
    stage: Mapped[str] = mapped_column(String(100), default="unspecified", nullable=False)
    grade: Mapped[str] = mapped_column(String(100), default="unspecified", nullable=False)
    subject: Mapped[str] = mapped_column(String(160), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(200))
    lesson: Mapped[str | None] = mapped_column(String(240))
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False)  # explanation/example/exercise/quiz/summary/glossary
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="ar", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", nullable=False)  # DRAFT/REVIEW/APPROVED/REJECTED/SUPERSEDED
    quality_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    sources: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    learning_objectives: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    tags: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    # Stage 9: the curriculum lesson/topic/concept this asset belongs to (None = not yet mapped).
    curriculum_node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("curriculum_nodes.id", ondelete="SET NULL"), index=True)
