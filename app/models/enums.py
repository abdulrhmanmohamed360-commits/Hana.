"""Enumerations stored as VARCHAR (non-native) so adding a value never needs an ALTER TYPE."""
from __future__ import annotations

import enum


class ClaimStatus(str, enum.Enum):
    RAW = "RAW"
    EXTRACTED = "EXTRACTED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    VERIFIED = "VERIFIED"
    CONFLICTED = "CONFLICTED"
    REJECTED = "REJECTED"
    OUTDATED = "OUTDATED"


class ClaimType(str, enum.Enum):
    DEFINITION = "DEFINITION"
    FACT = "FACT"
    EXPLANATION = "EXPLANATION"
    EXAMPLE = "EXAMPLE"
    FORMULA = "FORMULA"
    DATE = "DATE"
    RELATIONSHIP = "RELATIONSHIP"
    OTHER = "OTHER"


class SourcePolicy(str, enum.Enum):
    PENDING = "PENDING"  # registered, not yet reviewed -> never fetched
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


class SourceType(str, enum.Enum):
    WEBSITE = "WEBSITE"
    ENCYCLOPEDIA = "ENCYCLOPEDIA"
    JOURNAL = "JOURNAL"
    TEXTBOOK = "TEXTBOOK"
    DATASET = "DATASET"
    GOVERNMENT = "GOVERNMENT"
    UPLOAD = "UPLOAD"
    OTHER = "OTHER"


class DocumentStatus(str, enum.Enum):
    INGESTED = "INGESTED"
    EXTRACTED = "EXTRACTED"
    FAILED = "FAILED"


class EvidenceStance(str, enum.Enum):
    SUPPORTS = "SUPPORTS"
    OPPOSES = "OPPOSES"
    NEUTRAL = "NEUTRAL"


class KnowledgeItemType(str, enum.Enum):
    CONCEPT = "CONCEPT"
    DEFINITION = "DEFINITION"
    FACT = "FACT"
    EXPLANATION = "EXPLANATION"
    EXAMPLE = "EXAMPLE"
    FORMULA = "FORMULA"


class KnowledgeStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    VERIFIED = "VERIFIED"
    OUTDATED = "OUTDATED"
    RETIRED = "RETIRED"


class RelationType(str, enum.Enum):
    RELATED_TO = "RELATED_TO"
    DEPENDS_ON = "DEPENDS_ON"
    EXPLAINS = "EXPLAINS"
    CONTRADICTS = "CONTRADICTS"
    EXAMPLE_OF = "EXAMPLE_OF"
    PREREQUISITE_FOR = "PREREQUISITE_FOR"
    PART_OF = "PART_OF"
    EXTENDS = "EXTENDS"


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    DEAD_LETTER = "DEAD_LETTER"
    CANCELLED = "CANCELLED"
