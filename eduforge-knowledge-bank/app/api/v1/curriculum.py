from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.models import Curriculum, CurriculumNode
from app.schemas.stage9 import (CurriculumCreate, CurriculumUpdate, ImportRequest, NodeCreate, NodeUpdate, ResolveRequest)
from app.services import curriculum as svc

router = APIRouter(prefix="/curriculum", tags=["curriculum"], dependencies=[Depends(enforce_rate_limit)])


def _cur(c: Curriculum) -> dict:
    return {"id": str(c.id), "country": c.country, "education_system": c.education_system, "name": c.name, "language": c.language,
            "version": c.version, "status": c.status, "supersedes_id": str(c.supersedes_id) if c.supersedes_id else None,
            "description": c.description, "created_at": c.created_at}


def _node(n: CurriculumNode) -> dict:
    return {"id": str(n.id), "curriculum_id": str(n.curriculum_id), "parent_id": str(n.parent_id) if n.parent_id else None,
            "level": n.level, "name": n.name, "position": n.position, "description": n.description,
            "learning_objectives": n.learning_objectives}


@router.post("/resolve")
def resolve(body: ResolveRequest, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    """Identify the curriculum lesson a request refers to. Several matches are returned as AMBIGUOUS, never guessed."""
    r = svc.resolve_context(db, **body.model_dump())
    out = {"status": r.status, "reason": r.reason, "candidates": r.candidates}
    if r.node is not None:
        out.update(curriculum=_cur(r.curriculum), lesson=_node(r.node), path={k.lower(): v.name for k, v in r.path.items()},
                   context=svc.lesson_context(db, r.node))
    return out


@router.post("/link-assets")
def link_assets(limit: int = Query(default=500, ge=1, le=2000), db: Session = Depends(get_db),
                who: Principal = Depends(require_role(Role.EDITOR))):
    result = svc.link_existing_assets(db, actor=who.actor, limit=limit)
    db.commit()
    return result


@router.post("/nodes", status_code=201)
def create_node(body: NodeCreate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    node, _ = svc.add_node(db, body.curriculum_id, body.parent_id, body.level, body.name, actor=who.actor, position=body.position,
                           description=body.description, objectives=body.learning_objectives, concept_id=body.concept_id)
    db.commit()
    return _node(node)


@router.get("/nodes/{node_id}")
def get_node(node_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    node = svc.get_node(db, node_id)
    nodes = svc.load_nodes(db, node.curriculum_id)
    return {**_node(node), "path": [{"level": k, "name": v.name} for k, v in reversed(list(svc.path_of(nodes, node).items()))],
            "children": [_node(c) for c in svc.children(db, node.id, node.curriculum_id)]}


@router.get("/nodes/{node_id}/context")
def node_context(node_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    return svc.lesson_context(db, svc.get_node(db, node_id))


@router.patch("/nodes/{node_id}")
def patch_node(node_id: uuid.UUID, body: NodeUpdate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    node = svc.update_node(db, node_id, actor=who.actor, name=body.name, description=body.description,
                           objectives=body.learning_objectives, position=body.position, meta=body.meta)
    db.commit()
    return _node(node)


@router.delete("/nodes/{node_id}", status_code=204)
def remove_node(node_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    svc.delete_node(db, node_id, actor=who.actor)
    db.commit()


@router.post("", status_code=201)
def create_curriculum(body: CurriculumCreate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    c = svc.create_curriculum(db, actor=who.actor, **body.model_dump())
    db.commit()
    return _cur(c)


@router.get("")
def list_curricula(country: str | None = None, status: str | None = None, db: Session = Depends(get_db),
                   _: Principal = Depends(require_role(Role.READER))):
    stmt = select(Curriculum).order_by(Curriculum.country, Curriculum.name, Curriculum.version)
    if status:
        stmt = stmt.where(Curriculum.status == status.upper())
    rows = [c for c in db.scalars(stmt) if not country or svc.match_name(c.country, country)]
    return [_cur(c) for c in rows]


@router.get("/{curriculum_id}")
def get_curriculum(curriculum_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    return _cur(svc.get_curriculum(db, curriculum_id))


@router.patch("/{curriculum_id}")
def patch_curriculum(curriculum_id: uuid.UUID, body: CurriculumUpdate, db: Session = Depends(get_db),
                     who: Principal = Depends(require_role(Role.EDITOR))):
    c = svc.update_curriculum(db, curriculum_id, actor=who.actor, **body.model_dump())
    db.commit()
    return _cur(c)


@router.get("/{curriculum_id}/tree")
def curriculum_tree(curriculum_id: uuid.UUID, max_depth: int = Query(default=12, ge=1, le=12), db: Session = Depends(get_db),
                    _: Principal = Depends(require_role(Role.READER))):
    return {"curriculum": _cur(svc.get_curriculum(db, curriculum_id)), "tree": svc.tree(db, curriculum_id, max_depth)}


@router.post("/{curriculum_id}/import")
def import_curriculum(curriculum_id: uuid.UUID, body: ImportRequest, db: Session = Depends(get_db),
                      who: Principal = Depends(require_role(Role.EDITOR))):
    """Duplicate-safe: re-importing the same tree creates nothing."""
    result = svc.import_tree(db, curriculum_id, [i.model_dump(exclude_none=True) for i in body.items], actor=who.actor)
    db.commit()
    return result


@router.post("/{curriculum_id}/new-version", status_code=201)
def new_version(curriculum_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    c = svc.new_version(db, curriculum_id, actor=who.actor)
    db.commit()
    return _cur(c)


@router.post("/{curriculum_id}/activate")
def activate_version(curriculum_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.ADMIN))):
    result = svc.activate(db, curriculum_id, actor=who.actor)
    db.commit()
    return result
