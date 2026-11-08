from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SourcePolicy, SourceType


class SourceCreate(BaseModel):
    """New sources always start as policy=PENDING; an admin must approve them."""

    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=8, max_length=2048)
    source_type: SourceType = SourceType.WEBSITE
    authority_score: float = Field(default=0.5, ge=0, le=1)
    language: str | None = Field(default=None, max_length=16)
    rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    notes: str | None = None
    meta: dict = Field(default_factory=dict)


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    source_type: SourceType | None = None
    policy: SourcePolicy | None = None  # changing policy requires the admin role
    authority_score: float | None = Field(default=None, ge=0, le=1)
    language: str | None = Field(default=None, max_length=16)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=600)
    notes: str | None = None
    meta: dict | None = None


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    base_url: str
    host: str
    source_type: SourceType
    policy: SourcePolicy
    authority_score: float
    language: str | None
    rate_limit_per_minute: int
    notes: str | None
    meta: dict
    created_at: datetime
    updated_at: datetime


class UrlCheckRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


class UrlCheckResult(BaseModel):
    allowed: bool
    host: str | None
    reason: str


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor: str
    action: str
    entity_type: str
    entity_id: str | None
    request_id: str | None
    details: dict
    created_at: datetime
