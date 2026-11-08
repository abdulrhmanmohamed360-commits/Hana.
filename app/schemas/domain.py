from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import SLUG_PATTERN


class DomainCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    default_language: str = Field(default="en", min_length=2, max_length=16)
    education_level: str | None = Field(default=None, max_length=64)
    meta: dict = Field(default_factory=dict)


class DomainUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    default_language: str | None = Field(default=None, min_length=2, max_length=16)
    education_level: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    meta: dict | None = None


class DomainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    default_language: str
    education_level: str | None
    is_active: bool
    meta: dict
    created_at: datetime
    updated_at: datetime


class TopicCreate(BaseModel):
    domain_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    meta: dict = Field(default_factory=dict)


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    meta: dict | None = None


class TopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    domain_id: uuid.UUID
    parent_id: uuid.UUID | None
    slug: str
    name: str
    path: str
    depth: int
    description: str | None
    meta: dict
    created_at: datetime
    updated_at: datetime
