from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.core.url_policy import Resolver, UrlPolicyError, system_resolver, validate_url
from app.models import Source
from app.models.enums import SourcePolicy
from app.schemas.common import PageParams
from app.schemas.source import SourceCreate, SourceUpdate, UrlCheckResult
from app.services import audit
from app.services.common import commit_or_conflict, paginate


def get_source(db: Session, source_id: uuid.UUID) -> Source:
    obj = db.get(Source, source_id)
    if obj is None or obj.deleted_at is not None:
        raise NotFoundError("Source not found")
    return obj


def register_source(db: Session, data: SourceCreate, actor: str, settings: Settings) -> Source:
    try:  # syntax + literal-IP check only; full DNS validation happens at fetch time
        validated = validate_url(data.base_url, allow_private=settings.allow_private_networks, resolve=False)
    except UrlPolicyError as exc:
        raise InvalidInputError(f"Source URL rejected: {exc}") from exc
    if db.scalar(select(Source.id).where(Source.host == validated.host, Source.deleted_at.is_(None))):
        raise ConflictError(f"A source for host '{validated.host}' is already registered")
    payload = data.model_dump()
    obj = Source(**payload, host=validated.host, policy=SourcePolicy.PENDING)
    db.add(obj)
    db.flush()
    audit.record(db, actor=actor, action="source.register", entity_type="source", entity_id=obj.id,
                 details={"host": obj.host, "policy": obj.policy.value})
    commit_or_conflict(db, f"A source for host '{validated.host}' is already registered")
    return obj


def list_sources(db: Session, params: PageParams, *, policy: SourcePolicy | None = None):
    stmt = select(Source).where(Source.deleted_at.is_(None)).order_by(Source.host)
    if policy is not None:
        stmt = stmt.where(Source.policy == policy)
    return paginate(db, stmt, params)


def update_source(db: Session, source_id: uuid.UUID, data: SourceUpdate, actor: str) -> Source:
    obj = get_source(db, source_id)
    changes = data.model_dump(exclude_unset=True)
    previous_policy = obj.policy
    for key, value in changes.items():
        if value is None and key not in ("notes", "language"):
            continue
        setattr(obj, key, value)
    details: dict = {"fields": sorted(changes)}
    if obj.policy != previous_policy:
        details["policy_change"] = {"from": previous_policy.value, "to": obj.policy.value}
    audit.record(db, actor=actor, action="source.update", entity_type="source", entity_id=obj.id, details=details)
    commit_or_conflict(db, "Could not update source")
    return obj


def delete_source(db: Session, source_id: uuid.UUID, actor: str) -> None:
    obj = get_source(db, source_id)
    obj.deleted_at = datetime.now(timezone.utc)
    audit.record(db, actor=actor, action="source.delete", entity_type="source", entity_id=obj.id, details={"host": obj.host})
    commit_or_conflict(db, "Could not delete source")


def check_url(db: Session, url: str, settings: Settings, resolver: Resolver = system_resolver) -> UrlCheckResult:
    """Would the platform be allowed to fetch this URL? SSRF rules first, then the source allowlist/denylist."""
    try:
        validated = validate_url(url, allow_private=settings.allow_private_networks, resolve=True, resolver=resolver)
    except UrlPolicyError as exc:
        return UrlCheckResult(allowed=False, host=None, reason=f"ssrf_policy: {exc}")
    source = db.scalar(select(Source).where(Source.host == validated.host, Source.deleted_at.is_(None)))
    if source is None:
        return UrlCheckResult(allowed=False, host=validated.host, reason="host_not_registered")
    if source.policy == SourcePolicy.DENIED:
        return UrlCheckResult(allowed=False, host=validated.host, reason="source_denied")
    if source.policy != SourcePolicy.ALLOWED:
        return UrlCheckResult(allowed=False, host=validated.host, reason="source_pending_approval")
    return UrlCheckResult(allowed=True, host=validated.host, reason="allowed")
