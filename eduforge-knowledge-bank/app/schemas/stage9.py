"""Request contracts for the curriculum, coverage, graph, quality and continuous-learning APIs."""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["STAGE", "GRADE", "ACADEMIC_YEAR", "TERM", "SUBJECT", "UNIT", "LESSON", "TOPIC", "CONCEPT"]
Relation = Literal["PREREQUISITE_OF", "PART_OF", "RELATED_TO", "EXPLAINS", "USES_FORMULA", "HAS_EXAMPLE",
                   "HAS_QUESTION", "SUPPORTED_BY", "CONTRADICTS", "EXTENDS"]
EntityType = Literal["NODE", "ASSET", "CLAIM", "SOURCE"]


class _In(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class CurriculumCreate(_In):
    country: str = Field(min_length=1, max_length=100)
    education_system: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    language: str = Field(default="ar", min_length=2, max_length=16)
    description: str | None = Field(default=None, max_length=4000)
    status: Literal["DRAFT", "ACTIVE"] = "ACTIVE"


class CurriculumUpdate(_In):
    description: str | None = Field(default=None, max_length=4000)
    language: str | None = Field(default=None, min_length=2, max_length=16)
    meta: dict | None = None


class NodeCreate(_In):
    curriculum_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    level: Level
    name: str = Field(min_length=1, max_length=300)
    position: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=4000)
    learning_objectives: list[str] = Field(default_factory=list, max_length=30)
    concept_id: uuid.UUID | None = None


class NodeUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    learning_objectives: list[str] | None = Field(default=None, max_length=30)
    position: int | None = Field(default=None, ge=0)
    meta: dict | None = None


class ImportNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Level
    name: str = Field(min_length=1, max_length=300)
    description: str | None = None
    objectives: list[str] | None = Field(default=None, max_length=30)
    prerequisites: list[str] | None = Field(default=None, max_length=30)
    children: list["ImportNode"] = Field(default_factory=list)


class ImportRequest(_In):
    items: list[ImportNode] = Field(min_length=1, max_length=2000)


class ResolveRequest(_In):
    country: str
    grade: str
    subject: str
    lesson: str
    term: int | None = Field(default=None, ge=1, le=4)
    unit: str | None = None
    education_system: str | None = None
    curriculum_id: uuid.UUID | None = None


class RequirementIn(_In):
    asset_type: Literal["explanation", "definition", "formula", "example", "exercise", "quiz", "summary"]
    min_count: int = Field(default=1, ge=1, le=50)
    min_chars: int = Field(default=0, ge=0, le=100000)
    require_sources: bool = False
    require_answers: bool = False


class ProfilePut(_In):
    name: str = Field(min_length=1, max_length=200)
    subject_pattern: str | None = Field(default=None, max_length=160)
    requirements: list[RequirementIn] = Field(min_length=1, max_length=20)
    freshness_days: int | None = Field(default=730, ge=30, le=3650)


class EdgeCreate(_In):
    relation: Relation
    from_type: EntityType
    from_id: uuid.UUID
    to_type: EntityType
    to_id: uuid.UUID
    confidence: float = Field(default=1.0, ge=0, le=1)
    provenance: str | None = Field(default=None, max_length=2000)
    evidence_claim_id: uuid.UUID | None = None


class EdgeUpdate(_In):
    confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: str | None = Field(default=None, max_length=2000)


class LinkAsset(_In):
    asset_id: uuid.UUID
    node_id: uuid.UUID


class ScanScope(_In):
    country: str | None = None
    grade: str | None = None
    term: int | None = Field(default=None, ge=1, le=4)
    subject: str | None = None
    unit: str | None = None
    education_system: str | None = None
    curriculum_id: uuid.UUID | None = None

    def clean(self) -> dict:
        return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in self.model_dump().items() if v is not None}


class GapUpdate(_In):
    status: Literal["DISMISSED", "RESOLVED", "OPEN"]
    note: str | None = Field(default=None, max_length=2000)


class RevisionCreate(_In):
    content: str = Field(min_length=20, max_length=200000)
    reason: str = Field(min_length=3, max_length=1000)
    gap_id: uuid.UUID | None = None


class ScheduleCreate(_In):
    name: str = Field(min_length=1, max_length=120)
    scope: ScanScope = Field(default_factory=ScanScope)
    interval_hours: int = Field(default=24, ge=1, le=2160)
    enabled: bool = True


class SchedulePatch(_In):
    interval_hours: int | None = Field(default=None, ge=1, le=2160)
    enabled: bool | None = None
    scope: ScanScope | None = None
