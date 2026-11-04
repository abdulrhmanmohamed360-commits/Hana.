from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.logging import request_id_ctx
from app.models import AuditLog


def record(db: Session, *, actor: str, action: str, entity_type: str, entity_id: object | None = None,
           details: dict | None = None) -> AuditLog:
    """Add an audit row to the *current* transaction so it commits or rolls back with the change itself."""
    entry = AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        request_id=request_id_ctx.get(),
        details=details or {},
    )
    db.add(entry)
    return entry
