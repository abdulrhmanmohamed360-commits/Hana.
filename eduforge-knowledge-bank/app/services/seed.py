"""Initial domain registry definitions. These are *configuration* (which domains exist), not knowledge."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KnowledgeDomain
from app.schemas.domain import DomainCreate
from app.services import domains as domain_service

DEFAULT_DOMAINS: list[dict] = [
    {"slug": "mathematics", "name": "Mathematics"},
    {"slug": "physics", "name": "Physics"},
    {"slug": "chemistry", "name": "Chemistry"},
    {"slug": "biology", "name": "Biology"},
    {"slug": "general-science", "name": "General Science"},
    {"slug": "history", "name": "History"},
    {"slug": "geography", "name": "Geography"},
    {"slug": "arabic-language", "name": "Arabic Language", "default_language": "ar"},
    {"slug": "english-language", "name": "English Language"},
    {"slug": "computer-science", "name": "Computer Science"},
]


def seed_default_domains(db: Session, actor: str = "system") -> list[str]:
    """Idempotent: creates only the missing domains and returns the slugs it created."""
    created: list[str] = []
    for spec in DEFAULT_DOMAINS:
        exists = db.scalar(select(KnowledgeDomain.id).where(KnowledgeDomain.slug == spec["slug"], KnowledgeDomain.deleted_at.is_(None)))
        if exists:
            continue
        domain_service.create_domain(db, DomainCreate(**spec), actor)
        created.append(spec["slug"])
    return created
