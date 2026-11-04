from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.models import CurriculumNode, LearningAsset
from app.schemas.stage9 import EdgeCreate, EdgeUpdate, LinkAsset
from app.services import curriculum as cur_svc
from app.services import graph as svc

router = APIRouter(prefix="/graph", tags=["knowledge-graph"], dependencies=[Depends(enforce_rate_limit)])


@router.post("/edges", status_code=201)
def create_edge(body: EdgeCreate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    edge, _ = svc.create_edge(db, body.relation, body.from_type, body.from_id, body.to_type, body.to_id, actor=who.actor,
                              confidence=body.confidence, provenance=body.provenance, evidence_claim_id=body.evidence_claim_id)
    db.commit()
    return svc.edge_dict(edge)


@router.get("/edges/{edge_id}")
def get_edge(edge_id: uuid.UUID, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.READER))):
    edge = svc.get_edge(db, edge_id)
    if who.role < Role.EDITOR and not svc.visible_to_reader(db, [edge]):
        from app.core.errors import NotFoundError
        raise NotFoundError("edge not found")
    return svc.edge_dict(edge)


@router.patch("/edges/{edge_id}")
def patch_edge(edge_id: uuid.UUID, body: EdgeUpdate, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    edge = svc.update_edge(db, edge_id, actor=who.actor, confidence=body.confidence, provenance=body.provenance)
    db.commit()
    return svc.edge_dict(edge)


@router.delete("/edges/{edge_id}", status_code=204)
def delete_edge(edge_id: uuid.UUID, reason: str | None = Query(default=None, max_length=1000), db: Session = Depends(get_db),
                who: Principal = Depends(require_role(Role.EDITOR))):
    svc.delete_edge(db, edge_id, actor=who.actor, reason=reason)
    db.commit()


@router.get("/entities/{entity_id}/edges")
def entity_edges(entity_id: uuid.UUID, relation: str | None = None, direction: str = Query(default="both", pattern="^(in|out|both)$"),
                 limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db),
                 who: Principal = Depends(require_role(Role.READER))):
    edges = svc.edges_of(db, entity_id, relation=relation, direction=direction, limit=limit)
    if who.role < Role.EDITOR:
        edges = svc.visible_to_reader(db, edges)
    return {"entity_id": str(entity_id), "count": len(edges), "edges": [svc.edge_dict(e) for e in edges]}


@router.get("/nodes/{node_id}/related")
def related(node_id: uuid.UUID, depth: int = Query(default=2, ge=1, le=4), limit: int = Query(default=50, ge=1, le=200),
            db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    cur_svc.get_node(db, node_id)
    return {"node_id": str(node_id), "related": svc.related_nodes(db, node_id, depth=depth, limit=limit)}


@router.get("/nodes/{node_id}/prerequisites")
def prerequisites(node_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.READER))):
    cur_svc.get_node(db, node_id)
    chain = svc.prerequisite_chain(db, node_id)
    names = {n.id: n for n in db.scalars(select(CurriculumNode).where(CurriculumNode.id.in_(chain)))} if chain else {}
    return {"node_id": str(node_id), "learning_path": [{"node_id": str(i), "name": names[i].name, "level": names[i].level}
                                                        for i in chain if i in names]}


@router.get("/nodes/{node_id}/missing-prerequisites")
def missing(node_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.EDITOR))):
    cur_svc.get_node(db, node_id)
    return {"node_id": str(node_id), "missing": svc.missing_prerequisites(db, node_id)}


@router.post("/link-asset", status_code=201)
def link_asset(body: LinkAsset, db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    from app.core.errors import NotFoundError
    asset = db.get(LearningAsset, body.asset_id)
    if asset is None:
        raise NotFoundError("learning asset not found")
    edges = svc.link_asset(db, asset, body.node_id, actor=who.actor)
    db.commit()
    return {"asset_id": str(asset.id), "node_id": str(body.node_id), "edges": [svc.edge_dict(e) for e in edges]}
