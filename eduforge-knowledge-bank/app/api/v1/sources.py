from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError
from app.models.enums import SourcePolicy
from app.schemas.common import Page, PageParams, make_page, page_params
from app.schemas.source import SourceCreate, SourceOut, SourceUpdate, UrlCheckRequest, UrlCheckResult
from app.services import sources as svc

router = APIRouter(prefix="/sources", tags=["sources"], dependencies=[Depends(enforce_rate_limit)])


@router.post("", response_model=SourceOut, status_code=201)
def register_source(body: SourceCreate, db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                    who: Principal = Depends(require_role(Role.EDITOR))):
    return svc.register_source(db, body, who.actor, settings)


@router.get("", response_model=Page[SourceOut])
def list_sources(policy: SourcePolicy | None = Query(None), params: PageParams = Depends(page_params),
                 db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    items, total = svc.list_sources(db, params, policy=policy)
    return make_page(SourceOut, items, total, params)


@router.post("/check-url", response_model=UrlCheckResult)
def check_url(body: UrlCheckRequest, db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
              _: Principal = Depends(require_role(Role.EDITOR))):
    """Dry-run of the fetch policy (SSRF rules + source allowlist/denylist). Fetches nothing."""
    return svc.check_url(db, body.url, settings)


@router.get("/{source_id}", response_model=SourceOut)
def get_source(source_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    return svc.get_source(db, source_id)


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(source_id: uuid.UUID, body: SourceUpdate, db: Session = Depends(get_db),
                  who: Principal = Depends(require_role(Role.EDITOR))):
    if "policy" in body.model_fields_set and who.role < Role.ADMIN:
        raise ForbiddenError("Only admins may change a source policy (allow/deny)")
    return svc.update_source(db, source_id, body, who.actor)


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.ADMIN))):
    svc.delete_source(db, source_id, who.actor)
    return Response(status_code=204)
