from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.schemas.stage9 import RevisionCreate
from app.services import continuous_learning as cl
from app.services import quality as svc

router = APIRouter(prefix="/quality", tags=["knowledge-quality"], dependencies=[Depends(enforce_rate_limit)])


@router.get("/assets/{asset_id}")
def asset_quality(asset_id: uuid.UUID, persist: bool = True, db: Session = Depends(get_db),
                  who: Principal = Depends(require_role(Role.EDITOR))):
    """Explainable assessment: every dimension carries a reason. support_score is NOT a correctness probability."""
    out = svc.assess_asset(db, asset_id, persist=persist, actor=who.actor)
    db.commit()
    return out


@router.get("/assets/{asset_id}/history")
def asset_history(asset_id: uuid.UUID, limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db),
                  _: Principal = Depends(require_role(Role.EDITOR))):
    return {"asset_id": str(asset_id), "assessments": svc.history(db, "learning_asset", asset_id, limit)}


@router.get("/lessons/{node_id}")
def lesson_quality(node_id: uuid.UUID, persist: bool = True, db: Session = Depends(get_db),
                   who: Principal = Depends(require_role(Role.EDITOR))):
    out = svc.assess_lesson(db, node_id, persist=persist, actor=who.actor)
    db.commit()
    return out


@router.post("/assets/{asset_id}/revisions", status_code=201)
def create_revision(asset_id: uuid.UUID, body: RevisionCreate, db: Session = Depends(get_db),
                    who: Principal = Depends(require_role(Role.EDITOR))):
    """New DRAFT version. The approved original stays in force until an admin approves the revision."""
    rev = cl.create_revision(db, asset_id, actor=who.actor, content=body.content, reason=body.reason, gap_id=body.gap_id)
    db.commit()
    return {"revision_asset_id": str(rev.id), "version": rev.version, "status": rev.status, "supersedes": str(asset_id),
            "next_step": f"POST /api/v1/learning/assets/{rev.id}/approve (admin) after human review"}
