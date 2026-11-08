"""Stage 9 tables: curriculum hierarchy, coverage profiles, knowledge graph edges, gaps, scans, quality assessments.

Why new tables (existing ones were inspected first):
  * topics/concepts/knowledge_domains model a *subject-matter* tree, not country/system/grade/term/lesson hierarchy.
  * knowledge_relationships only links concepts to concepts; graph_edges links nodes, assets, claims and sources.
  * research_tasks / preparation_jobs are single work items; scan_runs are the history of whole evaluation sweeps.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, Uuid, text)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, SoftDeleteMixin, TimestampMixin, UUIDPkMixin, utcnow
from app.models.ops import _TaskColumns

_ACTIVE = text("deleted_at IS NULL")


class Curriculum(UUIDPkMixin, TimestampMixin, Base):
    """One national/system curriculum at one version. A new version is a new row (history is never overwritten)."""
    __tablename__ = "curricula"
    __table_args__ = (
        UniqueConstraint("country_key", "system_key", "name_key", "version", name="uq_curricula_identity"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_curricula_country_status", "country_key", "status"),
    )
    country: Mapped[str] = mapped_column(String(100), nullable=False)
    country_key: Mapped[str] = mapped_column(String(100), nullable=False)
    education_system: Mapped[str] = mapped_column(String(120), nullable=False)
    system_key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="ar", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)  # DRAFT | ACTIVE | ARCHIVED
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("curricula.id", ondelete="RESTRICT"))
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class CurriculumNode(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """STAGE/GRADE/ACADEMIC_YEAR/TERM/SUBJECT/UNIT/LESSON/TOPIC/CONCEPT as one adjacency-list tree."""
    __tablename__ = "curriculum_nodes"
    __table_args__ = (
        Index("uq_cnodes_child", "curriculum_id", "parent_id", "level", "name_key", unique=True,
              postgresql_where=text("deleted_at IS NULL AND parent_id IS NOT NULL"),
              sqlite_where=text("deleted_at IS NULL AND parent_id IS NOT NULL")),
        Index("uq_cnodes_root", "curriculum_id", "level", "name_key", unique=True,
              postgresql_where=text("deleted_at IS NULL AND parent_id IS NULL"),
              sqlite_where=text("deleted_at IS NULL AND parent_id IS NULL")),
        Index("ix_cnodes_parent", "parent_id", "position"),
        Index("ix_cnodes_level", "curriculum_id", "level"),
    )
    curriculum_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("curricula.id", ondelete="RESTRICT"), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("curriculum_nodes.id", ondelete="RESTRICT"))
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    name_key: Mapped[str] = mapped_column(String(300), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    learning_objectives: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    concept_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("concepts.id", ondelete="SET NULL"))
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class CoverageProfile(UUIDPkMixin, TimestampMixin, Base):
    """Operator-defined completeness criteria. Overrides the built-in profile with the same key."""
    __tablename__ = "coverage_profiles"
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    subject_pattern: Mapped[str | None] = mapped_column(String(160))   # matched against the subject name; None = by key only
    requirements: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    freshness_days: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class GraphEdge(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Typed relation between any two knowledge elements. Soft-deleted so history and re-adding both work."""
    __tablename__ = "graph_edges"
    __table_args__ = (
        Index("uq_graph_edges_active", "relation_type", "from_id", "to_id", unique=True,
              postgresql_where=_ACTIVE, sqlite_where=_ACTIVE),
        Index("ix_graph_edges_from", "from_id", "relation_type"),
        Index("ix_graph_edges_to", "to_id", "relation_type"),
        CheckConstraint("from_id <> to_id", name="no_self_edge"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_type: Mapped[str] = mapped_column(String(16), nullable=False)   # NODE | ASSET | CLAIM | SOURCE
    from_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    from_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    to_type: Mapped[str] = mapped_column(String(16), nullable=False)
    to_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    to_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    origin: Mapped[str] = mapped_column(String(24), default="MANUAL", nullable=False)  # MANUAL|IMPORT|PREPARE|SCAN
    provenance: Mapped[str | None] = mapped_column(Text)
    evidence_claim_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("extracted_claims.id", ondelete="SET NULL"))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class ScanSchedule(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "scan_schedules"
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    scope: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    interval_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class ScanRun(UUIDPkMixin, _TaskColumns, Base):
    """History of one knowledge-evaluation sweep (manual, scheduled or retry)."""
    __tablename__ = "scan_runs"
    __table_args__ = (Index("ix_scan_runs_queue", "status", "priority", "scheduled_at"),)
    kind: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    scope: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    stats: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    log: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("scan_schedules.id", ondelete="SET NULL"))
    retry_of_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("scan_runs.id", ondelete="SET NULL"))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class KnowledgeGap(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_gaps"
    __table_args__ = (
        Index("uq_knowledge_gaps_open", "fingerprint", unique=True,
              postgresql_where=text("status IN ('OPEN','IN_PROGRESS','REVIEW')"),
              sqlite_where=text("status IN ('OPEN','IN_PROGRESS','REVIEW')")),
        Index("ix_knowledge_gaps_status_priority", "status", "priority"),
    )
    gap_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)  # OPEN|IN_PROGRESS|REVIEW|RESOLVED|DISMISSED
    priority: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("curriculum_nodes.id", ondelete="SET NULL"), index=True)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("learning_assets.id", ondelete="SET NULL"), index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    research_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("research_tasks.id", ondelete="SET NULL"))
    preparation_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("preparation_jobs.id", ondelete="SET NULL"))
    scan_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("scan_runs.id", ondelete="SET NULL"))
    detected_by: Mapped[str] = mapped_column(String(128), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)


class QualityAssessment(UUIDPkMixin, Base):
    """Append-only snapshots so quality can be tracked over time; the rubric version is stored with each."""
    __tablename__ = "quality_assessments"
    __table_args__ = (Index("ix_quality_entity_time", "entity_type", "entity_id", "assessed_at"),)
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)   # learning_asset | curriculum_node
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rubric_version: Mapped[str] = mapped_column(String(8), nullable=False)
    support_score: Mapped[float] = mapped_column(Float, nullable=False)
    correctness: Mapped[str] = mapped_column(String(32), nullable=False)
    dimensions: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    flags: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
