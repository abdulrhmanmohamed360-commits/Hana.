from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.schemas.common import Page, PageParams, make_page, page_params
from app.schemas.domain import DomainCreate, DomainOut, DomainUpdate
from app.services import domains as svc

router = APIRouter(prefix="/domains", tags=["domains"], dependencies=[Depends(enforce_rate_limit)])


@router.post("", response_model=DomainOut, status_code=201)
def create_domain(body: DomainCreate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    return svc.create_domain(db, body, who.actor)


@router.get("", response_model=Page[DomainOut])
def list_domains(is_active: bool | None = Query(None), params: PageParams = Depends(page_params),
                 db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    items, total = svc.list_domains(db, params, is_active=is_active)
    return make_page(DomainOut, items, total, params)


@router.get("/{domain_id}", response_model=DomainOut)
def get_domain(domain_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    return svc.get_domain(db, domain_id)


@router.patch("/{domain_id}", response_model=DomainOut)
def update_domain(domain_id: uuid.UUID, body: DomainUpdate, db: Session = Depends(get_db),
                  who: Principal = Depends(require_role(Role.EDITOR))):
    return svc.update_domain(db, domain_id, body, who.actor)


@router.delete("/{domain_id}", status_code=204)
def delete_domain(domain_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.ADMIN))):
    svc.delete_domain(db, domain_id, who.actor)
    return Response(status_code=204)
