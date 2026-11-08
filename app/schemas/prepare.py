"""Public contracts for POST /api/v1/knowledge/prepare and the job status endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ContentType = Literal["explanation", "definitions", "formulas", "solved_examples", "exercises",
                      "questions", "answers", "summary", "sources"]
ALL_CONTENT: list[str] = ["explanation", "definitions", "formulas", "solved_examples", "exercises",
                          "questions", "answers", "summary", "sources"]
VerificationStatus = Literal["APPROVED", "MATH_VERIFIED", "SOURCE_SUPPORTED", "UNVERIFIED", "CONFLICTED", "REJECTED"]
DEFAULT_CONTENT: list[str] = ["explanation", "definitions", "solved_examples", "exercises", "questions", "answers",
                              "summary", "sources"]
# Severity order used when an item holds several claims: the WORST claim decides the item's status.
STATUS_SEVERITY = {"REJECTED": 5, "CONFLICTED": 4, "UNVERIFIED": 3, "SOURCE_SUPPORTED": 2, "MATH_VERIFIED": 1, "APPROVED": 0}


class PrepareRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    country: str = Field(min_length=1, max_length=100)
    curriculum: str = Field(default="National Curriculum", min_length=1, max_length=120)
    grade: str = Field(min_length=1, max_length=100)
    semester: int | None = Field(default=None, ge=1, le=4)
    subject: str = Field(min_length=1, max_length=160)
    unit: str | None = Field(default=None, max_length=200)
    lesson: str = Field(min_length=1, max_length=240)
    requested_content: list[ContentType] = Field(default_factory=lambda: list(DEFAULT_CONTENT), min_length=1, max_length=9)
    difficulty: Literal["beginner", "intermediate", "advanced"] = "beginner"
    language: str = Field(default="ar", min_length=2, max_length=16)
    mode: Literal["sync", "async"] = "sync"
    allow_generation: bool = True
    allow_research: bool = False
    curriculum_id: uuid.UUID | None = None   # optional: pin the curriculum when several match

    @field_validator("requested_content")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _research_needs_async(self):
        if self.allow_research and self.mode == "sync":
            raise ValueError("allow_research requires mode='async' (external research is too slow for a sync request)")
        return self


class Provenance(BaseModel):
    kind: Literal["approved_asset", "source_passage", "generator", "agent_draft"]
    ref_id: str | None = None
    title: str
    source_url: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    verification_status: str | None = None
    note: str | None = None


class ClaimReport(BaseModel):
    claim_id: str
    text: str
    claim_type: str
    verification_status: VerificationStatus
    method: str
    detail: str
    evidence_count: int = 0


class PackageItem(BaseModel):
    item_id: str
    content_type: ContentType
    title: str
    body: str
    data: dict | None = None
    language: str
    origin: Literal["approved_asset", "source_passage", "deterministic_generator", "agent_draft"]
    trust: Literal["APPROVED", "DRAFT", "SOURCE_MATERIAL"]
    verification_status: VerificationStatus
    claims: list[ClaimReport] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)


class GapReport(BaseModel):
    content_type: ContentType
    reason: str


class RejectedItem(BaseModel):
    content_type: ContentType
    title: str
    reasons: list[str]


class ConflictReport(BaseModel):
    kind: str
    claim_a: str
    source_a: str
    claim_b: str
    source_b: str
    detail: str


class RetrievalReport(BaseModel):
    mode: str
    signals_used: list[str]
    signals_unavailable: dict[str, str]
    passages_considered: int


class PreparePackage(BaseModel):
    request: PrepareRequest
    status: Literal["COMPLETE", "PARTIAL", "NOT_READY"]
    generated_at: datetime
    items: dict[str, list[PackageItem]]
    missing: list[GapReport]
    rejected: list[RejectedItem] = Field(default_factory=list)
    conflicts: list[ConflictReport] = Field(default_factory=list)
    trust_summary: dict[str, int]
    contains_unapproved_content: bool
    retrieval: RetrievalReport
    workflow: list[str]
    advisory_notes: list[str] = Field(default_factory=list)
    curriculum: dict | None = None            # resolved educational context (Curriculum Intelligence)
    knowledge_state: dict | None = None       # lesson coverage after this request (Coverage Engine)
    notice: str


class JobAccepted(BaseModel):
    job_id: uuid.UUID
    status: str
    status_url: str
    deduplicated: bool = False


class JobStatus(BaseModel):
    job_id: uuid.UUID
    kind: Literal["prepare"] = "prepare"
    status: str
    progress: dict
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: PreparePackage | None = None
