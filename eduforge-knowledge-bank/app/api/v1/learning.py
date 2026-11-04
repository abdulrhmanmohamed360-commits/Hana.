from __future__ import annotations

from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from app.services.audit import record as audit_record
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.models.learning import LearningAsset
from app.schemas.learning import LearningAssetCreate, LearningAssetOut, LearningGenerateRequest, LearningPackRequest
from app.services.authoring import generate_learning_content
from app.services.agent_pipeline import run_educational_pipeline

router = APIRouter(prefix="/learning", tags=["learning-bank"], dependencies=[Depends(enforce_rate_limit)])


@router.post("/assets", response_model=LearningAssetOut, status_code=201)
def create_asset(body: LearningAssetCreate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    data = body.model_dump()
    row = LearningAsset(**data, created_by=who.actor)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/generate", response_model=LearningAssetOut, status_code=201)
def generate_asset(body: LearningGenerateRequest, db: Session = Depends(get_db),
                   who: Principal = Depends(require_role(Role.EDITOR))):
    """Create an AI-authored DRAFT. Human/admin review is required before app delivery."""
    content = generate_learning_content(body)
    row = LearningAsset(
        education_system=body.education_system, stage=body.stage, grade=body.grade,
        subject=body.subject, unit=body.unit, lesson=body.lesson,
        asset_type=body.asset_type, title=f"{body.lesson or body.subject} - {body.asset_type}",
        content=content, language=body.language, status="DRAFT", quality_score=0.0,
        sources=body.source_references, learning_objectives=body.learning_objectives,
        tags=["ai-generated", "needs-review"], created_by=who.actor,
        meta={"authoring_engine": "openai-compatible", "review_required": True,
              "learner_level": body.learner_level},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/agent-pipeline", status_code=201)
def run_agent_pipeline(body: LearningGenerateRequest, db: Session = Depends(get_db),
                       who: Principal = Depends(require_role(Role.EDITOR))):
    """Run bounded research, analysis, teaching and verification agents; save a review-only draft."""
    result = run_educational_pipeline(db, body)
    evidence = result["evidence"]
    row = LearningAsset(
        education_system=body.education_system, stage=body.stage, grade=body.grade,
        subject=body.subject, unit=body.unit, lesson=body.lesson,
        asset_type=body.asset_type, title=f"{body.lesson or body.subject} - {body.asset_type}",
        content=result["content"], language=body.language, status="DRAFT", quality_score=0.0,
        sources=body.source_references + evidence,
        learning_objectives=body.learning_objectives,
        tags=["agent-pipeline", "needs-human-review"], created_by=who.actor,
        meta={"pipeline": result["pipeline"], "agent_reports": result["agent_reports"],
              "review_required": True, "evidence_count": len(evidence),
              "external_web_research": False},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "asset": LearningAssetOut.model_validate(row).model_dump(mode="json"),
        "agent_reports": result["agent_reports"],
        "evidence_count": len(evidence),
        "pipeline": result["pipeline"],
        "status": "DRAFT_REQUIRES_REVIEW",
    }


@router.post("/assets/{asset_id}/approve", response_model=LearningAssetOut)
def approve_asset(asset_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.ADMIN))):
    row = db.get(LearningAsset, asset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Learning asset not found")
    if row.status == "REJECTED":
        raise HTTPException(status_code=409, detail="Rejected assets cannot be approved")
    row.status = "APPROVED"
    row.reviewed_by = who.actor
    row.reviewed_at = datetime.now(timezone.utc)
    superseded = (row.meta or {}).get("supersedes_asset_id")
    if superseded:   # an approved revision retires the version it replaces; the old row is kept for history
        old = db.get(LearningAsset, uuid.UUID(superseded))
        if old is not None and old.status == "APPROVED":
            old.status = "SUPERSEDED"
            audit_record(db, actor=who.actor, action="asset.superseded", entity_type="learning_asset", entity_id=old.id,
                         details={"by": str(row.id)})
    db.commit()
    db.refresh(row)
    return row


@router.get("/catalog")
def learning_catalog(db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    rows = db.execute(select(LearningAsset.education_system, LearningAsset.stage, LearningAsset.grade, LearningAsset.subject)
                      .where(LearningAsset.status == "APPROVED").distinct()
                      .order_by(LearningAsset.education_system, LearningAsset.stage, LearningAsset.grade, LearningAsset.subject)).all()
    return {"count": len(rows), "curricula": [dict(education_system=r[0], stage=r[1], grade=r[2], subject=r[3]) for r in rows]}


@router.get("/assets", response_model=list[LearningAssetOut])
def list_assets(subject: str | None = None, grade: str | None = None, asset_type: str | None = None,
               status: str = Query(default="APPROVED", pattern="^(DRAFT|REVIEW|APPROVED|REJECTED|ALL)$"),
               limit: int = Query(default=50, ge=1, le=100), db: Session = Depends(get_db),
               _: Principal = Depends(require_role(Role.READER))):
    stmt = select(LearningAsset)
    if subject:
        stmt = stmt.where(LearningAsset.subject.ilike(f"%{subject}%"))
    if grade:
        stmt = stmt.where(LearningAsset.grade == grade)
    if asset_type:
        stmt = stmt.where(LearningAsset.asset_type == asset_type)
    if status != "ALL":
        stmt = stmt.where(LearningAsset.status == status)
    return list(db.scalars(stmt.order_by(LearningAsset.subject, LearningAsset.title).limit(limit)).all())


@router.post("/pack")
def request_learning_pack(body: LearningPackRequest, db: Session = Depends(get_db),
                          _: Principal = Depends(require_role(Role.READER))):
    """Return approved, prebuilt teaching assets for the app to arrange and design."""
    stmt = select(LearningAsset).where(
        LearningAsset.status == "APPROVED",
        LearningAsset.subject.ilike(body.subject),
        LearningAsset.education_system == body.education_system,
        LearningAsset.stage == body.stage,
        LearningAsset.grade == body.grade,
        LearningAsset.language == body.language,
        LearningAsset.asset_type.in_(body.include),
    )
    if body.unit:
        stmt = stmt.where(LearningAsset.unit.ilike(body.unit))
    if body.lesson:
        stmt = stmt.where(LearningAsset.lesson.ilike(body.lesson))
    assets = list(db.scalars(stmt.order_by(LearningAsset.asset_type, LearningAsset.title).limit(100)).all())
    found_types = sorted({a.asset_type for a in assets})
    missing_types = [kind for kind in body.include if kind not in found_types]
    return {
        "status": "READY" if assets and not missing_types else ("PARTIAL" if assets else "NOT_READY"),
        "curriculum": {"education_system": body.education_system, "stage": body.stage, "grade": body.grade,
                       "subject": body.subject, "unit": body.unit, "lesson": body.lesson, "language": body.language},
        "assets_count": len(assets), "available_types": found_types, "missing_types": missing_types,
        "assets": [LearningAssetOut.model_validate(a).model_dump(mode="json") for a in assets],
        "message": "The app should arrange and design these assets; the knowledge bank owns their educational content."
    }
