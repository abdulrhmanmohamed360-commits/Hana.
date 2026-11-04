"""Import every model so Base.metadata is complete (used by Alembic and tests)."""
from app.models.claim import EvidenceRecord, ExtractedClaim
from app.models.curriculum import (CoverageProfile, Curriculum, CurriculumNode, GraphEdge, KnowledgeGap,
                                   QualityAssessment, ScanRun, ScanSchedule)
from app.models.domain import Concept, KnowledgeDomain, Topic
from app.models.knowledge import Embedding, KnowledgeChunk, KnowledgeItem, KnowledgeRelationship, KnowledgeVersion
from app.models.learning import LearningAsset
from app.models.ops import AgentRun, AuditLog, DomainQualityMetric, IngestionJob, PreparationJob, ResearchTask, VerificationJob
from app.models.source import RawDocument, Source, SourceSnapshot

__all__ = [
    "CoverageProfile", "Curriculum", "CurriculumNode", "GraphEdge", "KnowledgeGap", "QualityAssessment", "ScanRun",
    "ScanSchedule",
    "AgentRun", "AuditLog", "Concept", "DomainQualityMetric", "Embedding", "EvidenceRecord", "ExtractedClaim",
    "IngestionJob", "PreparationJob", "LearningAsset", "KnowledgeChunk", "KnowledgeDomain", "KnowledgeItem", "KnowledgeRelationship", "KnowledgeVersion", "RawDocument",
    "ResearchTask", "Source", "SourceSnapshot", "Topic", "VerificationJob",
]
