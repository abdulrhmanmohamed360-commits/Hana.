"""Curriculum Intelligence: API + database (SQLite). PostgreSQL behaviour is covered by the Alembic/PG tests."""
from sqlalchemy import func, select

from app.models import AuditLog, CurriculumNode, GraphEdge
from tests.stage9_helpers import (API, GRADE, L_LIN, L_PRE, SUBJECT, TREE, TREE_NODE_COUNT, add_asset, node_ids, seed_curriculum)


def test_import_builds_hierarchy_objectives_and_prerequisite_edge(client, editor, reader, db_session):
    cid, result = seed_curriculum(client, editor)
    assert result["created"] == TREE_NODE_COUNT and result["prerequisite_edges_created"] == 1 and not result["unresolved_prerequisites"]
    tree = client.get(f"{API}/curriculum/{cid}/tree", headers=reader).json()["tree"]
    assert tree[0]["level"] == "STAGE" and tree[0]["children"][0]["level"] == "GRADE"
    lin, pre = node_ids(client, reader, cid, L_LIN, L_PRE)
    ctx = client.get(f"{API}/curriculum/nodes/{lin}/context", headers=reader).json()
    assert ctx["learning_objectives"] == ["حل معادلة خطية بمجهول واحد"]
    assert [p["name"] for p in ctx["prerequisites"]] == [L_PRE]
    nxt = client.get(f"{API}/curriculum/nodes/{pre}/context", headers=reader).json()["next_lessons"]
    assert [n["name"] for n in nxt] == [L_LIN]
    assert {c["name"] for c in ctx["topics_and_concepts"]} == {"المجهول", "طرفا المعادلة"}


def test_reimport_and_spelling_variants_never_duplicate(client, editor):
    cid, _ = seed_curriculum(client, editor)
    again = client.post(f"{API}/curriculum/{cid}/import", headers=editor, json={"items": TREE}).json()
    assert again["created"] == 0 and again["skipped_duplicates"] == TREE_NODE_COUNT
    unit, = node_ids(client, editor, cid, "الجبر")
    dup = client.post(f"{API}/curriculum/nodes", headers=editor, json={"curriculum_id": cid, "parent_id": unit, "level": "LESSON", "name": "المُعادلات الخطيّة!"})
    assert dup.status_code == 409
    assert client.post(f"{API}/curriculum", headers=editor, json={"country": "egypt", "education_system": "National", "name": "Egyptian National Curriculum"}).status_code == 409


def test_invalid_hierarchy_and_missing_parent_are_rejected(client, editor, reader):
    cid, _ = seed_curriculum(client, editor)
    grade, unit = node_ids(client, editor, cid, GRADE, "الجبر")
    assert client.post(f"{API}/curriculum/nodes", headers=editor, json={"curriculum_id": cid, "parent_id": grade, "level": "LESSON", "name": "x"}).status_code == 422
    assert client.post(f"{API}/curriculum/nodes", headers=editor, json={"curriculum_id": cid, "level": "LESSON", "name": "x"}).status_code == 422
    assert client.post(f"{API}/curriculum/nodes", headers=reader, json={"curriculum_id": cid, "parent_id": unit, "level": "LESSON", "name": "y"}).status_code == 403


def test_resolve_context_exact_ambiguous_and_not_found(client, editor, reader):
    cid, _ = seed_curriculum(client, editor)
    body = {"country": "Egypt", "grade": GRADE, "subject": SUBJECT, "lesson": L_LIN, "term": 1}
    ok = client.post(f"{API}/curriculum/resolve", headers=reader, json=body).json()
    assert ok["status"] == "RESOLVED" and ok["path"]["term"] == "الترم الأول" and ok["context"]["prerequisites"]
    assert client.post(f"{API}/curriculum/resolve", headers=reader, json={**body, "term": 2}).json()["status"] == "NOT_FOUND"
    assert client.post(f"{API}/curriculum/resolve", headers=reader, json={**body, "lesson": "درس غير موجود"}).json()["status"] == "NOT_FOUND"
    cid2, _ = seed_curriculum(client, editor, name="Egyptian Experimental Curriculum")
    amb = client.post(f"{API}/curriculum/resolve", headers=reader, json=body).json()
    assert amb["status"] == "AMBIGUOUS" and len(amb["candidates"]) == 2
    assert client.post(f"{API}/curriculum/resolve", headers=reader, json={**body, "curriculum_id": cid2}).json()["status"] == "RESOLVED"


def test_new_version_copies_structure_and_activation_carries_content_over(client, editor, admin, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    asset = add_asset(db_session, lin, "explanation")
    v2 = client.post(f"{API}/curriculum/{cid}/new-version", headers=editor)
    assert v2.status_code == 201 and v2.json()["version"] == 2 and v2.json()["status"] == "DRAFT"
    v2id = v2.json()["id"]
    assert db_session.scalar(select(func.count()).select_from(CurriculumNode).where(CurriculumNode.curriculum_id == v2id)) == TREE_NODE_COUNT
    assert db_session.scalar(select(func.count()).select_from(GraphEdge).where(GraphEdge.relation_type == "PREREQUISITE_OF", GraphEdge.deleted_at.is_(None))) == 2
    assert client.post(f"{API}/curriculum/{v2id}/activate", headers=editor).status_code == 403
    done = client.post(f"{API}/curriculum/{v2id}/activate", headers=admin).json()
    assert done["assets_carried_over"] >= 1
    assert client.get(f"{API}/curriculum/{cid}", headers=editor).json()["status"] == "ARCHIVED"
    db_session.refresh(asset)
    new_lin, = node_ids(client, editor, v2id, L_LIN)
    assert str(asset.curriculum_node_id) == new_lin
    assert db_session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "curriculum.activate")) == 1


def test_node_update_delete_rules_and_audit(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, concept, unit = node_ids(client, editor, cid, L_LIN, "المجهول", "الجبر")
    assert client.delete(f"{API}/curriculum/nodes/{lin}", headers=editor).status_code == 409           # has children
    assert client.patch(f"{API}/curriculum/nodes/{lin}", headers=editor, json={"learning_objectives": ["هدف 1", "هدف 2"]}).json()["learning_objectives"] == ["هدف 1", "هدف 2"]
    assert client.patch(f"{API}/curriculum/nodes/{lin}", headers=editor, json={"name": L_PRE}).status_code == 409   # sibling name clash
    assert client.delete(f"{API}/curriculum/nodes/{concept}", headers=editor).status_code == 204
    assert client.get(f"{API}/curriculum/nodes/{concept}", headers=editor).status_code == 404
    actions = set(db_session.scalars(select(AuditLog.action)))
    assert {"curriculum.node_update", "curriculum.node_delete", "curriculum.import"} <= actions


def test_backfill_links_legacy_assets_only_when_unambiguous(client, editor, db_session):
    from app.models import LearningAsset
    seed_curriculum(client, editor)
    a = LearningAsset(education_system="Egypt", stage="x", grade=GRADE, subject=SUBJECT, lesson=L_LIN, asset_type="explanation", title="t",
                      content="ن" * 300, language="ar", status="APPROVED", created_by="t")
    b = LearningAsset(education_system="Egypt", stage="x", grade=GRADE, subject=SUBJECT, lesson="درس مجهول", asset_type="explanation", title="t2",
                      content="ن" * 300, language="ar", status="APPROVED", created_by="t")
    db_session.add_all([a, b])
    db_session.commit()
    out = client.post(f"{API}/curriculum/link-assets", headers=editor).json()
    assert out == {"linked": 1, "ambiguous": 0, "unmatched": 1}
    db_session.refresh(a)
    assert a.curriculum_node_id is not None
