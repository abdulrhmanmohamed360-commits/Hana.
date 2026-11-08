from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.schemas.common import Page, PageParams, make_page, page_params
from app.schemas.domain import TopicCreate, TopicOut, TopicUpdate
from app.services import domains as svc

router = APIRouter(prefix="/topics", tags=["topics"], dependencies=[Depends(enforce_rate_limit)])


@router.post("", response_model=TopicOut, status_code=201)
def create_topic(body: TopicCreate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    return svc.create_topic(db, body, who.actor)


@router.get("", response_model=Page[TopicOut])
def list_topics(domain_id: uuid.UUID | None = Query(None), parent_id: uuid.UUID | None = Query(None),
                roots_only: bool = Query(False), params: PageParams = Depends(page_params),
                db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    items, total = svc.list_topics(db, params, domain_id=domain_id, parent_id=parent_id, roots_only=roots_only)
    return make_page(TopicOut, items, total, params)


@router.get("/{topic_id}", response_model=TopicOut)
def get_topic(topic_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    return svc.get_topic(db, topic_id)


@router.patch("/{topic_id}", response_model=TopicOut)
def update_topic(topic_id: uuid.UUID, body: TopicUpdate, db: Session = Depends(get_db),
                 who: Principal = Depends(require_role(Role.EDITOR))):
    return svc.update_topic(db, topic_id, body, who.actor)


@router.delete("/{topic_id}", status_code=204)
def delete_topic(topic_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    svc.delete_topic(db, topic_id, who.actor)
    return Response(status_code=204)
