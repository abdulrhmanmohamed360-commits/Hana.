from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models import KnowledgeDomain, Topic
from app.schemas.common import PageParams
from app.schemas.domain import DomainCreate, DomainUpdate, TopicCreate, TopicUpdate
from app.services import audit
from app.services.common import commit_or_conflict, paginate

MAX_TOPIC_DEPTH = 8


# ---------- domains ----------
def get_domain(db: Session, domain_id: uuid.UUID) -> KnowledgeDomain:
    obj = db.get(KnowledgeDomain, domain_id)
    if obj is None or obj.deleted_at is not None:
        raise NotFoundError("Domain not found")
    return obj


def create_domain(db: Session, data: DomainCreate, actor: str) -> KnowledgeDomain:
    exists = db.scalar(select(KnowledgeDomain.id).where(KnowledgeDomain.slug == data.slug, KnowledgeDomain.deleted_at.is_(None)))
    if exists:
        raise ConflictError(f"Domain slug '{data.slug}' already exists")
    obj = KnowledgeDomain(**data.model_dump())
    db.add(obj)
    db.flush()
    audit.record(db, actor=actor, action="domain.create", entity_type="knowledge_domain", entity_id=obj.id,
                 details={"slug": obj.slug})
    commit_or_conflict(db, f"Domain slug '{data.slug}' already exists")
    return obj


def list_domains(db: Session, params: PageParams, *, is_active: bool | None = None):
    stmt = select(KnowledgeDomain).where(KnowledgeDomain.deleted_at.is_(None)).order_by(KnowledgeDomain.slug)
    if is_active is not None:
        stmt = stmt.where(KnowledgeDomain.is_active == is_active)
    return paginate(db, stmt, params)


def update_domain(db: Session, domain_id: uuid.UUID, data: DomainUpdate, actor: str) -> KnowledgeDomain:
    obj = get_domain(db, domain_id)
    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key not in ("description", "education_level"):
            continue
        setattr(obj, key, value)
    audit.record(db, actor=actor, action="domain.update", entity_type="knowledge_domain", entity_id=obj.id,
                 details={"fields": sorted(changes)})
    commit_or_conflict(db, "Could not update domain")
    return obj


def delete_domain(db: Session, domain_id: uuid.UUID, actor: str) -> None:
    obj = get_domain(db, domain_id)
    has_topics = db.scalar(select(Topic.id).where(Topic.domain_id == obj.id, Topic.deleted_at.is_(None)).limit(1))
    if has_topics:
        raise ConflictError("Domain still has active topics; delete or move them first")
    obj.deleted_at = datetime.now(timezone.utc)
    audit.record(db, actor=actor, action="domain.delete", entity_type="knowledge_domain", entity_id=obj.id,
                 details={"slug": obj.slug})
    commit_or_conflict(db, "Could not delete domain")


# ---------- topics ----------
def get_topic(db: Session, topic_id: uuid.UUID) -> Topic:
    obj = db.get(Topic, topic_id)
    if obj is None or obj.deleted_at is not None:
        raise NotFoundError("Topic not found")
    return obj


def create_topic(db: Session, data: TopicCreate, actor: str) -> Topic:
    get_domain(db, data.domain_id)
    parent: Topic | None = None
    if data.parent_id is not None:
        parent = get_topic(db, data.parent_id)
        if parent.domain_id != data.domain_id:
            raise ConflictError("Parent topic belongs to a different domain")
    depth = 0 if parent is None else parent.depth + 1
    if depth >= MAX_TOPIC_DEPTH:
        raise ConflictError(f"Topic hierarchy is limited to {MAX_TOPIC_DEPTH} levels")
    path = data.slug if parent is None else f"{parent.path}/{data.slug}"
    clash = db.scalar(select(Topic.id).where(Topic.domain_id == data.domain_id, Topic.path == path, Topic.deleted_at.is_(None)))
    if clash:
        raise ConflictError(f"Topic path '{path}' already exists in this domain")
    obj = Topic(domain_id=data.domain_id, parent_id=data.parent_id, slug=data.slug, name=data.name,
                description=data.description, meta=data.meta, path=path, depth=depth)
    db.add(obj)
    db.flush()
    audit.record(db, actor=actor, action="topic.create", entity_type="topic", entity_id=obj.id, details={"path": path})
    commit_or_conflict(db, f"Topic path '{path}' already exists in this domain")
    return obj


def list_topics(db: Session, params: PageParams, *, domain_id: uuid.UUID | None = None,
                parent_id: uuid.UUID | None = None, roots_only: bool = False):
    stmt = select(Topic).where(Topic.deleted_at.is_(None)).order_by(Topic.path)
    if domain_id is not None:
        stmt = stmt.where(Topic.domain_id == domain_id)
    if parent_id is not None:
        stmt = stmt.where(Topic.parent_id == parent_id)
    elif roots_only:
        stmt = stmt.where(Topic.parent_id.is_(None))
    return paginate(db, stmt, params)


def update_topic(db: Session, topic_id: uuid.UUID, data: TopicUpdate, actor: str) -> Topic:
    obj = get_topic(db, topic_id)
    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key != "description":
            continue
        setattr(obj, key, value)
    audit.record(db, actor=actor, action="topic.update", entity_type="topic", entity_id=obj.id,
                 details={"fields": sorted(changes)})
    commit_or_conflict(db, "Could not update topic")
    return obj


def delete_topic(db: Session, topic_id: uuid.UUID, actor: str) -> None:
    obj = get_topic(db, topic_id)
    child = db.scalar(select(Topic.id).where(Topic.parent_id == obj.id, Topic.deleted_at.is_(None)).limit(1))
    if child:
        raise ConflictError("Topic still has active subtopics")
    obj.deleted_at = datetime.now(timezone.utc)
    audit.record(db, actor=actor, action="topic.delete", entity_type="topic", entity_id=obj.id, details={"path": obj.path})
    commit_or_conflict(db, "Could not delete topic")
