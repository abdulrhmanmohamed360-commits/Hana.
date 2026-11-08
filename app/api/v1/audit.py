from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.models import AuditLog
from app.schemas.common import Page, PageParams, make_page, page_params
from app.schemas.source import AuditLogOut
from app.services.common import paginate

router = APIRouter(prefix="/audit-logs", tags=["audit"], dependencies=[Depends(enforce_rate_limit)])


@router.get("", response_model=Page[AuditLogOut])
def list_audit_logs(entity_type: str | None = Query(None), action: str | None = Query(None),
                    params: PageParams = Depends(page_params), db: Session = Depends(get_db),
                    _: Principal = Depends(require_role(Role.ADMIN))):
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    items, total = paginate(db, stmt, params)
    return make_page(AuditLogOut, items, total, params)
