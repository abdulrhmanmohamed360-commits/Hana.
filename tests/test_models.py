import uuid

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.db.base import Base, utcnow
from app.models import KnowledgeDomain, KnowledgeItem, KnowledgeVersion, Source, Topic
from app.models.enums import KnowledgeItemType

EXPECTED_TABLES = {
    "knowledge_domains", "topics", "concepts", "sources", "source_snapshots", "raw_documents", "extracted_claims",
    "evidence_records", "knowledge_items", "knowledge_versions", "knowledge_relationships", "embeddings",
    "research_tasks", "ingestion_jobs", "verification_jobs", "agent_runs", "audit_logs", "domain_quality_metrics",
    "knowledge_chunks", "learning_assets",
}


def test_all_required_tables_exist(engine):
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_domain_slug_unique_among_active_but_reusable_after_soft_delete(db_session):
    first = KnowledgeDomain(slug="physics", name="Physics")
    db_session.add(first)
    db_session.commit()
    db_session.add(KnowledgeDomain(slug="physics", name="Dup"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    first.deleted_at = utcnow()
    db_session.commit()
    db_session.add(KnowledgeDomain(slug="physics", name="Physics v2"))
    db_session.commit()
    assert len(list(db_session.scalars(select(KnowledgeDomain)))) == 2


def test_foreign_keys_are_enforced(db_session):
    db_session.add(Topic(domain_id=uuid.uuid4(), slug="x", name="X", path="x"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_source_authority_check_constraint(db_session):
    db_session.add(Source(name="Bad", base_url="https://a.example", host="a.example", authority_score=1.5))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_only_one_current_version_per_knowledge_item(db_session):
    domain = KnowledgeDomain(slug="biology", name="Biology")
    db_session.add(domain)
    db_session.flush()
    item = KnowledgeItem(domain_id=domain.id, item_type=KnowledgeItemType.DEFINITION)
    db_session.add(item)
    db_session.flush()
    db_session.add(KnowledgeVersion(knowledge_item_id=item.id, version_number=1, content="v1", created_by="t", is_current=True))
    db_session.commit()
    db_session.add(KnowledgeVersion(knowledge_item_id=item.id, version_number=2, content="v2", created_by="t", is_current=True))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    # old version marked not-current + new current one is fine, and v1 content is preserved
    v1 = db_session.scalar(select(KnowledgeVersion).where(KnowledgeVersion.version_number == 1))
    v1.is_current = False
    db_session.add(KnowledgeVersion(knowledge_item_id=item.id, version_number=2, content="v2", created_by="t",
                                    is_current=True, supersedes_version_id=v1.id))
    db_session.commit()
    assert v1.content == "v1"


def test_version_numbers_unique_per_item(db_session):
    domain = KnowledgeDomain(slug="history", name="History")
    db_session.add(domain)
    db_session.flush()
    item = KnowledgeItem(domain_id=domain.id, item_type=KnowledgeItemType.FACT)
    db_session.add(item)
    db_session.flush()
    db_session.add(KnowledgeVersion(knowledge_item_id=item.id, version_number=1, content="a", created_by="t", is_current=False))
    db_session.commit()
    db_session.add(KnowledgeVersion(knowledge_item_id=item.id, version_number=1, content="b", created_by="t", is_current=False))
    with pytest.raises(IntegrityError):
        db_session.commit()
