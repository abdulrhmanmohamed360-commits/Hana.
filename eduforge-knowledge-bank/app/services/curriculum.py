"""Curriculum Intelligence: hierarchy CRUD, duplicate-free import, versioning, and educational-context resolution."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.engines.curriculum_rules import (HierarchyError, check_placement, match_name, name_key, term_number)
from app.models import Curriculum, CurriculumNode, GraphEdge, LearningAsset
from app.services import audit, graph

MAX_IMPORT_NODES = 5000


# ------------------------------------------------------------------ curricula
def create_curriculum(db: Session, *, country: str, education_system: str, name: str, language: str, actor: str,
                      description: str | None = None, status: str = "ACTIVE", meta: dict | None = None) -> Curriculum:
    ck, sk, nk = name_key(country), name_key(education_system), name_key(name)
    if not (ck and sk and nk):
        raise InvalidInputError("country, education_system and name must not be empty")
    if status not in ("DRAFT", "ACTIVE"):
        raise InvalidInputError("a new curriculum starts as DRAFT or ACTIVE")
    dup = db.scalar(select(Curriculum).where(Curriculum.country_key == ck, Curriculum.system_key == sk,
                                             Curriculum.name_key == nk))
    if dup is not None:
        raise ConflictError("this curriculum already exists; create a new version instead",
                            details={"curriculum_id": str(dup.id), "version": dup.version})
    cur = Curriculum(country=country.strip(), country_key=ck, education_system=education_system.strip(), system_key=sk,
                     name=name.strip(), name_key=nk, language=language, status=status, description=description,
                     created_by=actor, meta=meta or {})
    db.add(cur)
    db.flush()
    audit.record(db, actor=actor, action="curriculum.create", entity_type="curriculum", entity_id=cur.id,
                 details={"country": cur.country, "system": cur.education_system, "name": cur.name})
    return cur


def get_curriculum(db: Session, curriculum_id: uuid.UUID) -> Curriculum:
    cur = db.get(Curriculum, curriculum_id)
    if cur is None:
        raise NotFoundError("curriculum not found")
    return cur


def update_curriculum(db: Session, curriculum_id: uuid.UUID, *, actor: str, description: str | None = None,
                      language: str | None = None, meta: dict | None = None) -> Curriculum:
    cur = get_curriculum(db, curriculum_id)
    before = {"description": cur.description, "language": cur.language}
    if description is not None:
        cur.description = description
    if language is not None:
        cur.language = language
    if meta is not None:
        cur.meta = {**(cur.meta or {}), **meta}
    audit.record(db, actor=actor, action="curriculum.update", entity_type="curriculum", entity_id=cur.id,
                 details={"before": before})
    return cur


def new_version(db: Session, curriculum_id: uuid.UUID, *, actor: str) -> Curriculum:
    """Deep-copy structure + structural graph edges into a DRAFT next version. Content stays on the old nodes
    until the new version is activated (then unchanged lessons are carried over)."""
    old = get_curriculum(db, curriculum_id)
    latest = db.scalar(select(func.max(Curriculum.version)).where(
        Curriculum.country_key == old.country_key, Curriculum.system_key == old.system_key, Curriculum.name_key == old.name_key))
    new = Curriculum(country=old.country, country_key=old.country_key, education_system=old.education_system,
                     system_key=old.system_key, name=old.name, name_key=old.name_key, language=old.language,
                     version=(latest or old.version) + 1, status="DRAFT", supersedes_id=old.id,
                     description=old.description, created_by=actor, meta=dict(old.meta or {}))
    db.add(new)
    db.flush()
    nodes = list(db.scalars(select(CurriculumNode).where(CurriculumNode.curriculum_id == old.id,
                                                         CurriculumNode.deleted_at.is_(None))
                            .order_by(CurriculumNode.created_at)))
    mapping: dict[uuid.UUID, uuid.UUID] = {}
    by_parent: dict[uuid.UUID | None, list[CurriculumNode]] = {}
    for n in nodes:
        by_parent.setdefault(n.parent_id, []).append(n)
    queue = [(None, None)]
    while queue:
        old_parent, new_parent = queue.pop(0)
        for n in by_parent.get(old_parent, []):
            clone = CurriculumNode(curriculum_id=new.id, parent_id=new_parent, level=n.level, name=n.name,
                                   name_key=n.name_key, position=n.position, description=n.description,
                                   learning_objectives=list(n.learning_objectives or []), concept_id=n.concept_id,
                                   meta={**(n.meta or {}), "cloned_from": str(n.id)})
            db.add(clone)
            db.flush()
            mapping[n.id] = clone.id
            queue.append((n.id, clone.id))
    copied = 0
    if mapping:
        for e in db.scalars(select(GraphEdge).where(GraphEdge.deleted_at.is_(None), GraphEdge.from_type == "NODE",
                                                    GraphEdge.to_type == "NODE", GraphEdge.from_id.in_(mapping),
                                                    GraphEdge.to_id.in_(mapping))):
            graph.create_edge(db, e.relation_type, "NODE", mapping[e.from_id], "NODE", mapping[e.to_id], actor=actor,
                              origin="IMPORT", provenance=f"copied from curriculum v{old.version}",
                              confidence=e.confidence, on_duplicate="return")
            copied += 1
    audit.record(db, actor=actor, action="curriculum.new_version", entity_type="curriculum", entity_id=new.id,
                 details={"from": str(old.id), "version": new.version, "nodes": len(mapping), "edges": copied})
    return new


def activate(db: Session, curriculum_id: uuid.UUID, *, actor: str) -> dict:
    """Make this version ACTIVE, archive the other versions, and carry unchanged lessons' content over."""
    cur = get_curriculum(db, curriculum_id)
    for other in db.scalars(select(Curriculum).where(
            Curriculum.country_key == cur.country_key, Curriculum.system_key == cur.system_key,
            Curriculum.name_key == cur.name_key, Curriculum.id != cur.id, Curriculum.status == "ACTIVE")):
        other.status = "ARCHIVED"
    cur.status = "ACTIVE"
    moved = 0
    for n in db.scalars(select(CurriculumNode).where(CurriculumNode.curriculum_id == cur.id,
                                                     CurriculumNode.deleted_at.is_(None))):
        src = (n.meta or {}).get("cloned_from")
        if src:
            res = db.execute(update(LearningAsset).where(LearningAsset.curriculum_node_id == uuid.UUID(src))
                             .values(curriculum_node_id=n.id))
            moved += res.rowcount or 0
    audit.record(db, actor=actor, action="curriculum.activate", entity_type="curriculum", entity_id=cur.id,
                 details={"version": cur.version, "assets_carried_over": moved})
    return {"curriculum_id": str(cur.id), "version": cur.version, "assets_carried_over": moved}


# ------------------------------------------------------------------ nodes
def add_node(db: Session, curriculum_id: uuid.UUID, parent_id: uuid.UUID | None, level: str, name: str, *, actor: str,
             position: int | None = None, description: str | None = None, objectives: list[str] | None = None,
             concept_id: uuid.UUID | None = None, on_duplicate: str = "error") -> tuple[CurriculumNode, bool]:
    cur = get_curriculum(db, curriculum_id)
    parent = None
    if parent_id is not None:
        parent = db.get(CurriculumNode, parent_id)
        if parent is None or parent.deleted_at is not None or parent.curriculum_id != cur.id:
            raise NotFoundError("parent node not found in this curriculum")
    try:
        check_placement(parent.level if parent else None, level)
    except HierarchyError as exc:
        raise InvalidInputError(str(exc)) from exc
    key = name_key(name)
    if not key:
        raise InvalidInputError("name must not be empty")
    dup = db.scalar(select(CurriculumNode).where(
        CurriculumNode.curriculum_id == cur.id, CurriculumNode.level == level, CurriculumNode.name_key == key,
        CurriculumNode.parent_id == parent_id if parent_id else CurriculumNode.parent_id.is_(None),
        CurriculumNode.deleted_at.is_(None)))
    if dup is not None:
        if on_duplicate == "skip":
            return dup, False
        raise ConflictError(f"{level.lower()} '{name}' already exists here", details={"node_id": str(dup.id)})
    if position is None:
        position = (db.scalar(select(func.max(CurriculumNode.position)).where(
            CurriculumNode.parent_id == parent_id if parent_id else CurriculumNode.parent_id.is_(None),
            CurriculumNode.curriculum_id == cur.id)) or 0) + 1
    node = CurriculumNode(curriculum_id=cur.id, parent_id=parent_id, level=level, name=name.strip(), name_key=key,
                          position=position, description=description, learning_objectives=list(objectives or []),
                          concept_id=concept_id)
    db.add(node)
    db.flush()
    audit.record(db, actor=actor, action="curriculum.node_create", entity_type="curriculum_node", entity_id=node.id,
                 details={"level": level, "name": node.name, "curriculum_id": str(cur.id)})
    return node, True


def get_node(db: Session, node_id: uuid.UUID) -> CurriculumNode:
    node = db.get(CurriculumNode, node_id)
    if node is None or node.deleted_at is not None:
        raise NotFoundError("curriculum node not found")
    return node


def update_node(db: Session, node_id: uuid.UUID, *, actor: str, name: str | None = None, description: str | None = None,
                objectives: list[str] | None = None, position: int | None = None, meta: dict | None = None) -> CurriculumNode:
    node = get_node(db, node_id)
    before = {"name": node.name, "objectives": node.learning_objectives, "position": node.position}
    if name is not None and name_key(name) != node.name_key:
        clash = db.scalar(select(CurriculumNode).where(
            CurriculumNode.curriculum_id == node.curriculum_id, CurriculumNode.level == node.level,
            CurriculumNode.name_key == name_key(name), CurriculumNode.id != node.id,
            CurriculumNode.parent_id == node.parent_id if node.parent_id else CurriculumNode.parent_id.is_(None),
            CurriculumNode.deleted_at.is_(None)))
        if clash is not None:
            raise ConflictError("another sibling already has this name", details={"node_id": str(clash.id)})
        node.name, node.name_key = name.strip(), name_key(name)
    elif name is not None:
        node.name = name.strip()
    if description is not None:
        node.description = description
    if objectives is not None:
        node.learning_objectives = list(objectives)
    if position is not None:
        node.position = position
    if meta is not None:
        node.meta = {**(node.meta or {}), **meta}
    audit.record(db, actor=actor, action="curriculum.node_update", entity_type="curriculum_node", entity_id=node.id,
                 details={"before": before})
    return node


def delete_node(db: Session, node_id: uuid.UUID, *, actor: str) -> None:
    node = get_node(db, node_id)
    if db.scalar(select(func.count()).select_from(CurriculumNode).where(
            CurriculumNode.parent_id == node.id, CurriculumNode.deleted_at.is_(None))):
        raise ConflictError("delete or move the child nodes first")
    unlinked = db.execute(update(LearningAsset).where(LearningAsset.curriculum_node_id == node.id)
                          .values(curriculum_node_id=None)).rowcount or 0
    node.deleted_at = datetime.now(timezone.utc)
    audit.record(db, actor=actor, action="curriculum.node_delete", entity_type="curriculum_node", entity_id=node.id,
                 details={"level": node.level, "name": node.name, "assets_unlinked": unlinked})


def children(db: Session, node_id: uuid.UUID | None, curriculum_id: uuid.UUID) -> list[CurriculumNode]:
    cond = CurriculumNode.parent_id == node_id if node_id else CurriculumNode.parent_id.is_(None)
    return list(db.scalars(select(CurriculumNode).where(CurriculumNode.curriculum_id == curriculum_id, cond,
                                                        CurriculumNode.deleted_at.is_(None))
                           .order_by(CurriculumNode.position, CurriculumNode.name)))


def load_nodes(db: Session, curriculum_id: uuid.UUID) -> dict[uuid.UUID, CurriculumNode]:
    return {n.id: n for n in db.scalars(select(CurriculumNode).where(CurriculumNode.curriculum_id == curriculum_id,
                                                                     CurriculumNode.deleted_at.is_(None)))}


def path_of(nodes: dict[uuid.UUID, CurriculumNode], node: CurriculumNode) -> dict[str, CurriculumNode]:
    """Ancestors (and the node itself) keyed by level."""
    out, cur, guard = {}, node, 0
    while cur is not None and guard < 15:
        out[cur.level] = cur
        cur = nodes.get(cur.parent_id) if cur.parent_id else None
        guard += 1
    return out


def tree(db: Session, curriculum_id: uuid.UUID, max_depth: int = 12) -> list[dict]:
    nodes = load_nodes(db, curriculum_id)
    by_parent: dict = {}
    for n in nodes.values():
        by_parent.setdefault(n.parent_id, []).append(n)

    def build(parent, depth):
        if depth > max_depth:
            return []
        return [{"id": str(n.id), "level": n.level, "name": n.name, "position": n.position,
                 "learning_objectives": n.learning_objectives, "children": build(n.id, depth + 1)}
                for n in sorted(by_parent.get(parent, []), key=lambda x: (x.position, x.name))]
    return build(None, 0)


# ------------------------------------------------------------------ import (duplicate-safe)
def import_tree(db: Session, curriculum_id: uuid.UUID, items: list[dict], *, actor: str) -> dict:
    """items: [{level, name, objectives?, description?, prerequisites?: [lesson names], children?: [...]}].
    Existing nodes (same parent, level and normalised name) are reused, never duplicated; re-importing is a no-op."""
    created = skipped = 0
    pending_prereqs: list[tuple[CurriculumNode, list[str]]] = []

    def walk(entries: list[dict], parent: CurriculumNode | None) -> None:
        nonlocal created, skipped
        for e in entries:
            if created + skipped >= MAX_IMPORT_NODES:
                raise InvalidInputError(f"import exceeds {MAX_IMPORT_NODES} nodes")
            node, was_created = add_node(db, curriculum_id, parent.id if parent else None, e["level"], e["name"],
                                         actor=actor, description=e.get("description"), objectives=e.get("objectives"),
                                         on_duplicate="skip")
            created += was_created
            skipped += (not was_created)
            if was_created is False and e.get("objectives") and not node.learning_objectives:
                node.learning_objectives = list(e["objectives"])
            if e.get("prerequisites"):
                pending_prereqs.append((node, list(e["prerequisites"])))
            walk(e.get("children", []), node)

    walk(items, None)
    nodes = load_nodes(db, curriculum_id)
    edges_created, unresolved = 0, []
    for node, names in pending_prereqs:
        for pname in names:
            hits = [n for n in nodes.values() if n.id != node.id and n.level in ("LESSON", "TOPIC", "CONCEPT", "SUBJECT")
                    and n.name_key == name_key(pname)]
            if len(hits) != 1:
                unresolved.append({"node": node.name, "prerequisite": pname, "matches": len(hits)})
                continue
            _, was_new = graph.create_edge(db, "PREREQUISITE_OF", "NODE", hits[0].id, "NODE", node.id, actor=actor,
                                           origin="IMPORT", provenance="declared in curriculum import", on_duplicate="return")
            edges_created += was_new
    audit.record(db, actor=actor, action="curriculum.import", entity_type="curriculum", entity_id=curriculum_id,
                 details={"created": created, "skipped_duplicates": skipped, "prerequisite_edges": edges_created,
                          "unresolved_prerequisites": len(unresolved)})
    return {"created": created, "skipped_duplicates": skipped, "prerequisite_edges_created": edges_created,
            "unresolved_prerequisites": unresolved}


# ------------------------------------------------------------------ context resolution
@dataclass
class ResolvedContext:
    status: str                                   # RESOLVED | AMBIGUOUS | NOT_FOUND
    node: CurriculumNode | None = None
    curriculum: Curriculum | None = None
    path: dict[str, CurriculumNode] = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""


def _best(matches: list[CurriculumNode], wanted: str) -> list[CurriculumNode]:
    exact = [n for n in matches if n.name_key == name_key(wanted)]
    return exact or matches


def resolve_context(db: Session, *, country: str, grade: str, subject: str, lesson: str, term: int | None = None,
                    unit: str | None = None, education_system: str | None = None,
                    curriculum_id: uuid.UUID | None = None) -> ResolvedContext:
    """Find the one lesson node a request refers to. Several matches are reported, never guessed."""
    if curriculum_id:
        curricula = [get_curriculum(db, curriculum_id)]
    else:
        curricula = [c for c in db.scalars(select(Curriculum).where(Curriculum.status == "ACTIVE"))
                     if match_name(c.country, country) and (not education_system or match_name(c.education_system, education_system))]
    if not curricula:
        return ResolvedContext("NOT_FOUND", reason="no ACTIVE curriculum for this country/system")
    found: list[tuple[Curriculum, CurriculumNode, dict]] = []
    for cur in curricula:
        nodes = load_nodes(db, cur.id)
        lessons = _best([n for n in nodes.values() if n.level == "LESSON" and match_name(n.name, lesson)], lesson)
        for node in lessons:
            p = path_of(nodes, node)
            if "GRADE" not in p or not match_name(p["GRADE"].name, grade):
                continue
            if "SUBJECT" not in p or not match_name(p["SUBJECT"].name, subject):
                continue
            if unit and ("UNIT" not in p or not match_name(p["UNIT"].name, unit)):
                continue
            if term is not None and "TERM" in p and term_number(p["TERM"].name) not in (None, term):
                continue
            found.append((cur, node, p))
    if not found:
        return ResolvedContext("NOT_FOUND", reason="no lesson matches the requested grade/subject/lesson")
    if len(found) > 1:
        return ResolvedContext("AMBIGUOUS", candidates=[
            {"curriculum_id": str(c.id), "curriculum": c.name, "version": c.version, "node_id": str(n.id),
             "path": [p[l].name for l in ("STAGE", "GRADE", "TERM", "SUBJECT", "UNIT", "LESSON") if l in p]}
            for c, n, p in found], reason="several curricula/lessons match; pass curriculum_id or unit to disambiguate")
    cur, node, p = found[0]
    return ResolvedContext("RESOLVED", node=node, curriculum=cur, path=p)


def lesson_context(db: Session, node: CurriculumNode) -> dict:
    """Objectives, prerequisites, concepts and neighbouring lessons for one lesson node."""
    chain = graph.prerequisite_chain(db, node.id)
    names = {n.id: n for n in db.scalars(select(CurriculumNode).where(CurriculumNode.id.in_(chain)))} if chain else {}
    followers = db.scalars(select(GraphEdge.to_id).where(GraphEdge.relation_type == "PREREQUISITE_OF",
                                                         GraphEdge.from_id == node.id, GraphEdge.deleted_at.is_(None)))
    fnodes = {n.id: n for n in db.scalars(select(CurriculumNode).where(CurriculumNode.id.in_(list(followers))))} if followers else {}
    concepts = list(db.scalars(select(CurriculumNode).where(CurriculumNode.parent_id == node.id,
                                                            CurriculumNode.level.in_(["TOPIC", "CONCEPT"]),
                                                            CurriculumNode.deleted_at.is_(None)).order_by(CurriculumNode.position)))
    return {"node_id": str(node.id), "name": node.name, "learning_objectives": node.learning_objectives,
            "prerequisites": [{"node_id": str(i), "name": names[i].name} for i in chain if i in names],
            "missing_prerequisites": graph.missing_prerequisites(db, node.id),
            "next_lessons": [{"node_id": str(n.id), "name": n.name} for n in fnodes.values()],
            "topics_and_concepts": [{"node_id": str(c.id), "name": c.name, "level": c.level} for c in concepts]}


def link_existing_assets(db: Session, *, actor: str, limit: int = 500) -> dict:
    """Backfill: map assets that predate Stage 9 (free-text country/grade/subject/lesson) onto curriculum lesson nodes.
    Only unambiguous matches are linked; ambiguous or unmatched assets are reported, never guessed."""
    from app.models import LearningAsset as _A
    linked = ambiguous = unmatched = 0
    for a in db.scalars(select(_A).where(_A.curriculum_node_id.is_(None), _A.status.notin_(["REJECTED", "SUPERSEDED"])).limit(limit)):
        r = resolve_context(db, country=a.education_system, grade=a.grade, subject=a.subject, lesson=a.lesson or "",
                            unit=a.unit) if a.lesson else ResolvedContext("NOT_FOUND")
        if r.status == "RESOLVED":
            graph.link_asset(db, a, r.node.id, actor=actor, origin="IMPORT")
            linked += 1
        elif r.status == "AMBIGUOUS":
            ambiguous += 1
        else:
            unmatched += 1
    audit.record(db, actor=actor, action="curriculum.link_assets", entity_type="curriculum", entity_id=None,
                 details={"linked": linked, "ambiguous": ambiguous, "unmatched": unmatched})
    return {"linked": linked, "ambiguous": ambiguous, "unmatched": unmatched}
