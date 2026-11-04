"""Knowledge graph service: validated, de-duplicated, provenance-carrying edges with efficient local traversals.

No traversal ever loads the whole graph: each hop is one indexed query over a frontier of ids, depth-limited.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.engines import graph_rules as gr
from app.models import CurriculumNode, ExtractedClaim, GraphEdge, LearningAsset, Source
from app.services import audit

ENDPOINT_NODE_LEVELS = {"SUBJECT", "LESSON", "TOPIC", "CONCEPT"}
_TRAVERSAL_RELATIONS = ("PREREQUISITE_OF", "PART_OF", "RELATED_TO", "EXTENDS")


def entity_kind(db: Session, etype: str, eid: uuid.UUID) -> str:
    """Existence check + kind derivation. Raises 404/422 so the API never stores edges to nothing."""
    if etype == "NODE":
        node = db.get(CurriculumNode, eid)
        if node is None or node.deleted_at is not None:
            raise NotFoundError(f"curriculum node {eid} not found")
        if node.level not in ENDPOINT_NODE_LEVELS:
            raise InvalidInputError(f"nodes of level {node.level} cannot be graph endpoints; use one of {sorted(ENDPOINT_NODE_LEVELS)}")
        return node.level
    if etype == "ASSET":
        asset = db.get(LearningAsset, eid)
        if asset is None:
            raise NotFoundError(f"learning asset {eid} not found")
        kind = gr.asset_kind(asset.asset_type)
        if kind is None:
            raise InvalidInputError(f"asset type {asset.asset_type!r} has no graph kind")
        return kind
    if etype == "CLAIM":
        if db.get(ExtractedClaim, eid) is None:
            raise NotFoundError(f"claim {eid} not found")
        return "CLAIM"
    if etype == "SOURCE":
        src = db.get(Source, eid)
        if src is None or src.deleted_at is not None:
            raise NotFoundError(f"source {eid} not found")
        return "SOURCE"
    raise InvalidInputError(f"unknown entity type {etype!r}; expected NODE, ASSET, CLAIM or SOURCE")


def _active(stmt):
    return stmt.where(GraphEdge.deleted_at.is_(None))


def _frontier_walk(db: Session, relation: str, start: uuid.UUID, direction: str, max_depth: int) -> tuple[dict, dict]:
    """BFS by frontier. Returns (distance by id, adjacency {id: set(neighbour ids)}) in the walk direction."""
    dist: dict[uuid.UUID, int] = {}
    adj: dict[uuid.UUID, set[uuid.UUID]] = {}
    frontier = {start}
    col_from, col_to = (GraphEdge.from_id, GraphEdge.to_id) if direction == "out" else (GraphEdge.to_id, GraphEdge.from_id)
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        rows = db.execute(_active(select(col_from, col_to).where(GraphEdge.relation_type == relation,
                                                                   col_from.in_(frontier)))).all()
        nxt = set()
        for a, b in rows:
            adj.setdefault(a, set()).add(b)
            if b not in dist and b != start:
                dist[b] = depth
                nxt.add(b)
        frontier = nxt
    return dist, adj


def create_edge(db: Session, relation: str, from_type: str, from_id: uuid.UUID, to_type: str, to_id: uuid.UUID, *,
                actor: str, origin: str = "MANUAL", provenance: str | None = None, confidence: float = 1.0,
                evidence_claim_id: uuid.UUID | None = None, on_duplicate: str = "error") -> tuple[GraphEdge, bool]:
    """Validate, canonicalise, de-duplicate and store an edge. on_duplicate: 'error' (409) or 'return'."""
    if not 0.0 <= confidence <= 1.0:
        raise InvalidInputError("confidence must be between 0 and 1")
    from_kind, to_kind = entity_kind(db, from_type, from_id), entity_kind(db, to_type, to_id)
    try:
        gr.validate_edge(relation, from_kind, to_kind, from_id == to_id)
    except gr.GraphRuleError as exc:
        raise InvalidInputError(str(exc)) from exc
    if relation in gr.SYMMETRIC and str(from_id) > str(to_id):
        (from_type, from_id, from_kind), (to_type, to_id, to_kind) = (to_type, to_id, to_kind), (from_type, from_id, from_kind)
    existing = db.scalar(_active(select(GraphEdge).where(GraphEdge.relation_type == relation,
                                                          GraphEdge.from_id == from_id, GraphEdge.to_id == to_id)))
    if existing is not None:
        if on_duplicate == "return":
            return existing, False
        raise ConflictError("this relation already exists", details={"edge_id": str(existing.id)})
    if relation in gr.ACYCLIC:
        dist, _ = _frontier_walk(db, relation, to_id, "out", 50)   # would from_id become reachable from to_id?
        if from_id in dist:
            raise InvalidInputError(f"{relation} edge would create a cycle")
    edge = GraphEdge(relation_type=relation, from_type=from_type, from_id=from_id, from_kind=from_kind,
                     to_type=to_type, to_id=to_id, to_kind=to_kind, confidence=confidence, origin=origin,
                     provenance=provenance, evidence_claim_id=evidence_claim_id, created_by=actor)
    db.add(edge)
    db.flush()
    audit.record(db, actor=actor, action="graph.edge_create", entity_type="graph_edge", entity_id=edge.id,
                 details={"relation": relation, "from": f"{from_kind}:{from_id}", "to": f"{to_kind}:{to_id}", "origin": origin})
    return edge, True


def get_edge(db: Session, edge_id: uuid.UUID) -> GraphEdge:
    edge = db.get(GraphEdge, edge_id)
    if edge is None or edge.deleted_at is not None:
        raise NotFoundError("edge not found")
    return edge


def update_edge(db: Session, edge_id: uuid.UUID, *, actor: str, confidence: float | None = None,
                provenance: str | None = None) -> GraphEdge:
    edge = get_edge(db, edge_id)
    before = {"confidence": edge.confidence, "provenance": edge.provenance}
    if confidence is not None:
        if not 0.0 <= confidence <= 1.0:
            raise InvalidInputError("confidence must be between 0 and 1")
        edge.confidence = confidence
    if provenance is not None:
        edge.provenance = provenance
    audit.record(db, actor=actor, action="graph.edge_update", entity_type="graph_edge", entity_id=edge.id,
                 details={"before": before, "after": {"confidence": edge.confidence, "provenance": edge.provenance}})
    return edge


def delete_edge(db: Session, edge_id: uuid.UUID, *, actor: str, reason: str | None = None) -> None:
    edge = get_edge(db, edge_id)
    edge.deleted_at = datetime.now(timezone.utc)
    audit.record(db, actor=actor, action="graph.edge_delete", entity_type="graph_edge", entity_id=edge.id,
                 details={"relation": edge.relation_type, "from": str(edge.from_id), "to": str(edge.to_id),
                          "origin": edge.origin, "created_by": edge.created_by, "reason": reason})


def edges_of(db: Session, entity_id: uuid.UUID, *, relation: str | None = None, direction: str = "both",
             limit: int = 100) -> list[GraphEdge]:
    cond = {"out": GraphEdge.from_id == entity_id, "in": GraphEdge.to_id == entity_id}.get(
        direction, or_(GraphEdge.from_id == entity_id, GraphEdge.to_id == entity_id))
    stmt = _active(select(GraphEdge).where(cond)).order_by(GraphEdge.created_at.desc()).limit(max(1, min(limit, 500)))
    if relation:
        stmt = stmt.where(GraphEdge.relation_type == relation)
    return list(db.scalars(stmt))


def related_nodes(db: Session, node_id: uuid.UUID, *, depth: int = 2, limit: int = 50) -> list[dict]:
    """Concepts/topics/lessons connected through RELATED_TO / EXTENDS / PREREQUISITE_OF / PART_OF (both directions)."""
    depth = max(1, min(depth, 4))
    seen: dict[uuid.UUID, dict] = {}
    frontier = {node_id}
    for d in range(1, depth + 1):
        if not frontier:
            break
        rows = db.execute(_active(select(GraphEdge.from_id, GraphEdge.to_id, GraphEdge.relation_type,
                                         GraphEdge.from_type, GraphEdge.to_type).where(
            GraphEdge.relation_type.in_(_TRAVERSAL_RELATIONS),
            or_(GraphEdge.from_id.in_(frontier), GraphEdge.to_id.in_(frontier))))).all()
        nxt = set()
        for a, b, rel, ta, tb in rows:
            for here, there, tthere in ((a, b, tb), (b, a, ta)):
                if here in frontier and there != node_id and there not in seen and tthere == "NODE":
                    seen[there] = {"id": there, "distance": d, "via": rel}
                    nxt.add(there)
        frontier = nxt
    if not seen:
        return []
    nodes = {n.id: n for n in db.scalars(select(CurriculumNode).where(CurriculumNode.id.in_(seen),
                                                                      CurriculumNode.deleted_at.is_(None)))}
    out = [{"node_id": str(i), "name": nodes[i].name, "level": nodes[i].level, "distance": v["distance"], "via": v["via"]}
           for i, v in seen.items() if i in nodes]
    return sorted(out, key=lambda r: (r["distance"], r["name"]))[:limit]


def prerequisite_chain(db: Session, node_id: uuid.UUID, max_depth: int = 10) -> list[uuid.UUID]:
    """Everything the node depends on, ordered so each prerequisite comes after its own prerequisites."""
    dist, adj_in = _frontier_walk(db, "PREREQUISITE_OF", node_id, "in", max_depth)
    # adj_in[x] = nodes that are prerequisites of x (walking `in` flips the columns)
    needs = {k: {str(v) for v in vs} for k, vs in adj_in.items()}
    order = gr.learning_path({str(k): v for k, v in needs.items()}, str(node_id), max_depth)
    return [uuid.UUID(x) for x in order]


def missing_prerequisites(db: Session, node_id: uuid.UUID) -> list[dict]:
    """Prerequisites in the learning chain that no APPROVED explanation/definition/summary covers."""
    chain = prerequisite_chain(db, node_id)
    if not chain:
        return []
    covered = set(db.scalars(select(LearningAsset.curriculum_node_id).where(
        LearningAsset.curriculum_node_id.in_(chain), LearningAsset.status == "APPROVED",
        LearningAsset.asset_type.in_(["explanation", "definition", "glossary", "summary"]))))
    covered |= set(db.scalars(_active(select(GraphEdge.to_id).where(
        GraphEdge.relation_type == "EXPLAINS", GraphEdge.to_id.in_(chain), GraphEdge.from_type == "ASSET",
        GraphEdge.from_id.in_(select(LearningAsset.id).where(LearningAsset.status == "APPROVED"))))))
    nodes = {n.id: n for n in db.scalars(select(CurriculumNode).where(CurriculumNode.id.in_(chain)))}
    return [{"node_id": str(i), "name": nodes[i].name, "level": nodes[i].level}
            for i in chain if i not in covered and i in nodes]


def link_asset(db: Session, asset: LearningAsset, node_id: uuid.UUID, *, actor: str, origin: str = "MANUAL") -> list[GraphEdge]:
    """Attach an asset to a lesson/topic/concept: sets curriculum_node_id and adds the typed graph edge(s)."""
    node = db.get(CurriculumNode, node_id)
    if node is None or node.deleted_at is not None:
        raise NotFoundError("curriculum node not found")
    if node.level not in {"LESSON", "TOPIC", "CONCEPT"}:
        raise InvalidInputError("assets can only be linked to a LESSON, TOPIC or CONCEPT node")
    kind = gr.asset_kind(asset.asset_type)
    if kind is None:
        raise InvalidInputError(f"asset type {asset.asset_type!r} cannot be linked")
    asset.curriculum_node_id = node.id
    created: list[GraphEdge] = []
    relation = gr.LESSON_LINK[kind]
    pair = (("ASSET", asset.id, "NODE", node.id) if relation == "EXPLAINS" else ("NODE", node.id, "ASSET", asset.id))
    edge, _ = create_edge(db, relation, pair[0], pair[1], pair[2], pair[3], actor=actor, origin=origin,
                          provenance=f"asset {asset.id} linked to {node.level.lower()} '{node.name}'", on_duplicate="return")
    created.append(edge)
    hosts = {urlparse(str(s.get("url") or s.get("source_url") or "")).hostname for s in (asset.sources or []) if isinstance(s, dict)}
    for src in db.scalars(select(Source).where(Source.host.in_([h for h in hosts if h]), Source.deleted_at.is_(None))):
        e, _ = create_edge(db, "SUPPORTED_BY", "ASSET", asset.id, "SOURCE", src.id, actor=actor, origin=origin,
                           provenance="host of a cited URL is a registered source", on_duplicate="return")
        created.append(e)
    return created


def edge_dict(e: GraphEdge) -> dict:
    return {"id": str(e.id), "relation": e.relation_type, "from": {"type": e.from_type, "id": str(e.from_id), "kind": e.from_kind},
            "to": {"type": e.to_type, "id": str(e.to_id), "kind": e.to_kind}, "confidence": e.confidence,
            "origin": e.origin, "provenance": e.provenance, "created_by": e.created_by, "created_at": e.created_at}


def visible_to_reader(db: Session, edges: list[GraphEdge]) -> list[GraphEdge]:
    """Readers see structure and APPROVED content only: never drafts, claims or review-state assets."""
    asset_ids = {e.from_id for e in edges if e.from_type == "ASSET"} | {e.to_id for e in edges if e.to_type == "ASSET"}
    approved = set(db.scalars(select(LearningAsset.id).where(LearningAsset.id.in_(asset_ids),
                                                             LearningAsset.status == "APPROVED"))) if asset_ids else set()
    return [e for e in edges if "CLAIM" not in (e.from_type, e.to_type)
            and all(not (t == "ASSET" and i not in approved) for t, i in ((e.from_type, e.from_id), (e.to_type, e.to_id)))]
