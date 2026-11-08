from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AssetType = Literal["explanation", "definition", "example", "formula", "exercise", "quiz", "summary", "glossary", "lesson_plan"]


class LearningAssetCreate(BaseModel):
    education_system: str = Field(default="general", min_length=1, max_length=100)
    stage: str = Field(default="unspecified", min_length=1, max_length=100)
    grade: str = Field(default="unspecified", min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=160)
    unit: str | None = Field(default=None, max_length=200)
    lesson: str | None = Field(default=None, max_length=240)
    asset_type: AssetType
    title: str = Field(min_length=2, max_length=300)
    content: str = Field(min_length=2, max_length=100000)
    language: str = Field(default="ar", min_length=2, max_length=16)
    status: Literal["DRAFT", "REVIEW"] = "DRAFT"
    quality_score: float = Field(default=0, ge=0, le=1)
    sources: list[dict] = Field(default_factory=list, max_length=30)
    learning_objectives: list[str] = Field(default_factory=list, max_length=30)
    tags: list[str] = Field(default_factory=list, max_length=50)
    meta: dict = Field(default_factory=dict)


class LearningAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    education_system: str
    stage: str
    grade: str
    subject: str
    unit: str | None
    lesson: str | None
    asset_type: str
    title: str
    content: str
    language: str
    status: str
    quality_score: float
    sources: list
    learning_objectives: list
    tags: list
    version: int
    created_at: datetime
    updated_at: datetime


class LearningPackRequest(BaseModel):
    education_system: str = Field(default="general", max_length=100)
    stage: str = Field(default="unspecified", max_length=100)
    grade: str = Field(default="unspecified", max_length=100)
    subject: str = Field(min_length=1, max_length=160)
    unit: str | None = Field(default=None, max_length=200)
    lesson: str | None = Field(default=None, max_length=240)
    include: list[AssetType] = Field(default_factory=lambda: ["explanation", "example", "exercise", "summary"], max_length=9)
    language: str = Field(default="ar", max_length=16)


class LearningGenerateRequest(BaseModel):
    """Request an AI-authored draft for later human review."""
    education_system: str = Field(default="general", min_length=1, max_length=100)
    stage: str = Field(default="unspecified", min_length=1, max_length=100)
    grade: str = Field(default="unspecified", min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=160)
    unit: str | None = Field(default=None, max_length=200)
    lesson: str | None = Field(default=None, max_length=240)
    asset_type: AssetType = "explanation"
    language: str = Field(default="ar", min_length=2, max_length=16)
    learning_objectives: list[str] = Field(default_factory=list, max_length=20)
    learner_level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    additional_instructions: str = Field(default="", max_length=4000)
    source_references: list[dict] = Field(default_factory=list, max_length=20)
