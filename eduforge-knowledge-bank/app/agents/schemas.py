"""Strict contracts for agent input/output. Critical operations only ever consume these validated models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ContentTypeName = Literal["explanation", "definitions", "formulas", "solved_examples", "exercises",
                          "questions", "answers", "summary", "sources"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class EvidenceRef(_Strict):
    evidence_id: str            # E1, E2 ... assigned by the workflow; agents may cite only these
    title: str
    content: str
    chunk_id: str
    document_id: str
    source_url: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    verification_status: str = "SOURCE_MATERIAL"


class KeyPoint(_Strict):
    text: str = Field(min_length=3, max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)


class ResearchReport(_Strict):
    summary: str = Field(min_length=3, max_length=4000)
    key_points: list[KeyPoint] = Field(default_factory=list, max_length=20)
    missing_content: list[ContentTypeName] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list, max_length=5)


class Concept(_Strict):
    name: str = Field(min_length=1, max_length=200)
    why_it_matters: str = Field(default="", max_length=500)


class AnalysisPlan(_Strict):
    prerequisites: list[str] = Field(default_factory=list, max_length=15)
    concepts: list[Concept] = Field(default_factory=list, max_length=20)
    misconceptions: list[str] = Field(default_factory=list, max_length=15)
    teaching_sequence: list[str] = Field(default_factory=list, max_length=15)


class DefinitionDraft(_Strict):
    term: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=3, max_length=1500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)


class StepDraft(_Strict):
    equation: str = Field(min_length=1, max_length=200)
    explanation: str = Field(default="", max_length=500)


class SolvedExampleDraft(_Strict):
    title: str = Field(default="", max_length=200)
    problem: str = Field(min_length=1, max_length=200)
    variable: str = Field(min_length=1, max_length=2)
    steps: list[StepDraft] = Field(min_length=1, max_length=12)
    answer: str = Field(min_length=1, max_length=60)


class ExerciseDraft(_Strict):
    prompt: str = Field(min_length=1, max_length=500)
    equation: str = Field(min_length=1, max_length=200)
    variable: str = Field(min_length=1, max_length=2)
    answer: str = Field(min_length=1, max_length=60)
    steps: list[StepDraft] = Field(default_factory=list, max_length=12)


class TeacherDraft(_Strict):
    explanation: str | None = Field(default=None, max_length=12000)
    definitions: list[DefinitionDraft] = Field(default_factory=list, max_length=20)
    formulas: list[str] = Field(default_factory=list, max_length=20)
    summary: str | None = Field(default=None, max_length=4000)
    evidence_ids_used: list[str] = Field(default_factory=list, max_length=30)
    solved_examples: list[SolvedExampleDraft] = Field(default_factory=list, max_length=6)
    exercises: list[ExerciseDraft] = Field(default_factory=list, max_length=12)


class ReviewIssue(_Strict):
    claim: str = Field(max_length=1000)
    problem: str = Field(max_length=1000)
    severity: Literal["low", "medium", "high"] = "medium"


class ReviewVerdict(_Strict):
    """Advisory only: this verdict never changes a verification status."""
    overall: Literal["no_issues_found", "issues_found"]
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=30)
