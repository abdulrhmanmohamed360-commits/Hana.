from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, UUIDPkMixin, utcnow
from app.models.enums import TaskStatus
from app.models.source import _enum


class _TaskColumns(TimestampMixin):
    """Shared state-machine columns for every persistent job type."""

    status: Mapped[TaskStatus] = mapped_column(_enum(TaskStatus), default=TaskStatus.PENDING, nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)


class ResearchTask(UUIDPkMixin, _TaskColumns, Base):
    __tablename__ = "research_tasks"
    __table_args__ = (Index("ix_research_tasks_queue", "status", "priority", "scheduled_at"),)

    domain_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("knowledge_domains.id", ondelete="SET NULL"), index=True)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("topics.id", ondelete="SET NULL"))
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class PreparationJob(UUIDPkMixin, _TaskColumns, Base):
    """Persistent asynchronous run of the knowledge-preparation workflow (POST /knowledge/prepare, mode=async)."""
    __tablename__ = "preparation_jobs"
    __table_args__ = (Index("ix_preparation_jobs_queue", "status", "priority", "scheduled_at"),)

    request: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    progress: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONType)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class IngestionJob(UUIDPkMixin, _TaskColumns, Base):
    __tablename__ = "ingestion_jobs"

    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    research_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("research_tasks.id", ondelete="SET NULL"))
    input_uri: Mapped[str | None] = mapped_column(String(4096))
    documents_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), default="system", nullable=False)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    # Processing progress and crash-safe ownership (migration 0007)
    stage: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationJob(UUIDPkMixin, _TaskColumns, Base):
    __tablename__ = "verification_jobs"

    claim_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("extracted_claims.id", ondelete="CASCADE"), nullable=False, index=True)
    methodology: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class AgentRun(UUIDPkMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_agent_started", "agent_name", "started_at"),)

    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(_enum(TaskStatus), default=TaskStatus.RUNNING, nullable=False)
    task_type: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    input: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    output: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class AuditLog(UUIDPkMixin, Base):
    """Append-only trail of state-changing operations. Never updated or deleted by application code."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_created", "created_at"),
    )

    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DomainQualityMetric(UUIDPkMixin, Base):
    __tablename__ = "domain_quality_metrics"
    __table_args__ = (Index("ix_dqm_domain_metric_time", "domain_id", "metric_name", "measured_at"),)

    domain_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("knowledge_domains.id", ondelete="CASCADE"), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    details: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
