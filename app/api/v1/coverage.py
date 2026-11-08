from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.core.errors import NotFoundError
from app.engines.coverage_rules import PROFILES
from app.models import CoverageProfile
from app.schemas.stage9 import ProfilePut
from app.services import audit, coverage
from app.services import curriculum as cur_svc

router = APIRouter(prefix="/coverage", tags=["coverage"], dependencies=[Depends(enforce_rate_limit)])


@router.get("/report")
def report(country: str | None = None, grade: str | None = None, term: int | None = Query(default=None, ge=1, le=4),
           subject: str | None = None, unit: str | None = None, curriculum_id: uuid.UUID | None = None,
           education_system: str | None = None, details: bool = True, db: Session = Depends(get_db),
           _: Principal = Depends(require_role(Role.EDITOR))):
    """Coverage by country/grade/term/subject/unit. Editor-only: it exposes draft, pending-review and unverified state.
    Only APPROVED content counts towards coverage."""
    return coverage.coverage_report(db, details=details, country=country, grade=grade, term=term, subject=subject, unit=unit,
                                    curriculum_id=curriculum_id, education_system=education_system)


@router.get("/lessons/{node_id}")
def lesson(node_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.EDITOR))):
    node = cur_svc.get_node(db, node_id)
    subj = cur_svc.path_of(cur_svc.load_nodes(db, node.curriculum_id), node).get("SUBJECT")
    return {"lesson_id": str(node.id), "lesson": node.name, **coverage.lesson_state(db, node, subj.name if subj else "")}


@router.get("/profiles")
def list_profiles(db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.EDITOR))):
    custom = {p.key: p for p in db.scalars(select(CoverageProfile))}
    out = [{"key": k, "source": "custom" if k in custom else "built-in", "freshness_days": (custom[k].freshness_days if k in custom else p.freshness_days),
            "requirements": (custom[k].requirements if k in custom else [r.__dict__ for r in p.requirements])} for k, p in PROFILES.items()]
    out += [{"key": k, "source": "custom", "subject_pattern": p.subject_pattern, "freshness_days": p.freshness_days,
             "requirements": p.requirements} for k, p in custom.items() if k not in PROFILES]
    return out


@router.put("/profiles/{key}")
def put_profile(key: str, body: ProfilePut, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.ADMIN))):
    row = db.scalar(select(CoverageProfile).where(CoverageProfile.key == key))
    reqs = [r.model_dump() for r in body.requirements]
    if row is None:
        row = CoverageProfile(key=key, name=body.name, subject_pattern=body.subject_pattern, requirements=reqs,
                              freshness_days=body.freshness_days, created_by=who.actor)
        db.add(row)
    else:
        row.name, row.subject_pattern, row.requirements, row.freshness_days = body.name, body.subject_pattern, reqs, body.freshness_days
    audit.record(db, actor=who.actor, action="coverage.profile_put", entity_type="coverage_profile", entity_id=key,
                 details={"requirements": len(reqs)})
    db.commit()
    return {"key": key, "requirements": reqs}


@router.delete("/profiles/{key}", status_code=204)
def delete_profile(key: str, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.ADMIN))):
    row = db.scalar(select(CoverageProfile).where(CoverageProfile.key == key))
    if row is None:
        raise NotFoundError("custom profile not found (built-in profiles cannot be deleted)")
    audit.record(db, actor=who.actor, action="coverage.profile_delete", entity_type="coverage_profile", entity_id=key, details={})
    db.delete(row)
    db.commit()
