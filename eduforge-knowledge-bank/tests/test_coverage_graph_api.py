"""Knowledge Coverage Engine and Knowledge Graph: API + database."""
import uuid

from sqlalchemy import select

from app.models import AuditLog, ExtractedClaim, LearningAsset, Source
from app.models.enums import ClaimStatus, ClaimType, SourcePolicy
from tests.stage9_helpers import (API, GRADE, L_LIN, L_PRE, SUBJECT, add_asset, full_math_lesson, node_ids, seed_curriculum)


def report(client, headers, **params):
    r = client.get(f"{API}/coverage/report", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ coverage
def test_empty_curriculum_reports_every_lesson_missing(client, editor):
    seed_curriculum(client, editor)
    r = report(client, editor)
    t = r["totals"]
    assert (t["total_subjects"], t["total_lessons"], t["missing_lessons"], t["covered_lessons"], t["coverage_percentage"]) == (1, 2, 2, 0, 0.0)
    assert {x["status"] for x in r["lessons"]} == {"MISSING"}


def test_short_text_or_title_does_not_make_a_lesson_complete(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    add_asset(db_session, lin, "explanation", chars=15)
    row = next(x for x in report(client, editor)["lessons"] if x["lesson"] == L_LIN)
    assert row["status"] == "INCOMPLETE" and row["score"] == 0.0
    assert any("too short" in u["reason"] for u in row["unmet"])


def test_unapproved_content_is_never_counted_as_coverage(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    full_math_lesson(db_session, lin, status="DRAFT")
    r = report(client, editor)
    row = next(x for x in r["lessons"] if x["lesson"] == L_LIN)
    assert row["status"] == "PENDING_REVIEW" and row["score"] == 0.0 and row["pending_reviews"] > 0
    assert r["totals"]["covered_lessons"] == 0 and r["totals"]["pending_reviews"] > 0


def test_complete_lesson_and_percentage_by_scope(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    full_math_lesson(db_session, lin)
    r = report(client, editor)
    assert r["totals"]["covered_lessons"] == 1 and r["totals"]["coverage_percentage"] == 50.0
    assert report(client, editor, subject=SUBJECT, term=1, grade=GRADE, unit="الجبر")["totals"]["total_lessons"] == 2
    assert report(client, editor, grade="الصف الأول الثانوي")["totals"]["total_lessons"] == 0
    assert report(client, editor, term=2)["totals"]["total_lessons"] == 0
    assert r["by_subject"][0]["subject"] == SUBJECT and r["by_subject"][0]["coverage_percentage"] == 50.0


def test_outdated_missing_sources_and_missing_answers_are_detected(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    full_math_lesson(db_session, lin, age_days=2000)
    assert next(x for x in report(client, editor)["lessons"] if x["lesson"] == L_LIN)["status"] == "OUTDATED"
    cid2, _ = seed_curriculum(client, editor, name="Second")
    lin2, = node_ids(client, editor, cid2, L_LIN)
    add_asset(db_session, lin2, "explanation", sources=False)
    add_asset(db_session, lin2, "exercise", chars=60, answers=False)
    row = next(x for x in report(client, editor, curriculum_id=cid2)["lessons"] if x["lesson"] == L_LIN)
    reasons = " | ".join(u["reason"] for u in row["unmet"])
    assert "no sources" in reasons and "answers cannot be checked" in reasons
    assert len(row["findings"]["exercises_without_answers"]) == 1 and len(row["findings"]["assets_needing_sources"]) == 1


def test_concepts_without_explanation_and_formulas_without_examples(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, unknown = node_ids(client, editor, cid, L_LIN, "المجهول")
    exp = add_asset(db_session, unknown, "explanation")             # explains one concept only
    add_asset(db_session, lin, "formula", chars=60)
    row = next(x for x in report(client, editor)["lessons"] if x["lesson"] == L_LIN)
    assert row["findings"]["concepts_without_explanation"] == ["طرفا المعادلة"]
    assert len(row["findings"]["formulas_without_examples"]) == 1


def test_custom_profile_changes_the_definition_of_complete(client, editor, admin, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    add_asset(db_session, lin, "explanation", chars=300)
    assert next(x for x in report(client, editor)["lessons"] if x["lesson"] == L_LIN)["status"] == "INCOMPLETE"
    body = {"name": "light math", "subject_pattern": SUBJECT, "freshness_days": 730,
            "requirements": [{"asset_type": "explanation", "min_count": 1, "min_chars": 200, "require_sources": True}]}
    assert client.put(f"{API}/coverage/profiles/light-math", headers=editor, json=body).status_code == 403
    assert client.put(f"{API}/coverage/profiles/light-math", headers=admin, json=body).status_code == 200
    row = next(x for x in report(client, editor)["lessons"] if x["lesson"] == L_LIN)
    assert row["status"] == "COMPLETE" and row["profile"] == "light-math"


def test_coverage_report_is_not_available_to_readers(client, reader):
    assert client.get(f"{API}/coverage/report", headers=reader).status_code == 403


# ------------------------------------------------------------------ graph
def edge(client, headers, relation, ft, fid, tt, tid, **kw):
    return client.post(f"{API}/graph/edges", headers=headers, json={"relation": relation, "from_type": ft, "from_id": fid, "to_type": tt, "to_id": tid, **kw})


def test_edge_validation_duplicates_and_symmetry(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, pre, unk, side = node_ids(client, editor, cid, L_LIN, L_PRE, "المجهول", "طرفا المعادلة")
    ok = edge(client, editor, "RELATED_TO", "NODE", unk, "NODE", side, provenance="textbook p.12")
    assert ok.status_code == 201 and ok.json()["provenance"] == "textbook p.12" and ok.json()["created_by"] == "editor"
    assert edge(client, editor, "RELATED_TO", "NODE", unk, "NODE", side).status_code == 409
    assert edge(client, editor, "RELATED_TO", "NODE", side, "NODE", unk).status_code == 409        # symmetric duplicate
    assert edge(client, editor, "PREREQUISITE_OF", "NODE", pre, "NODE", lin).status_code == 409    # created by the import
    assert edge(client, editor, "PREREQUISITE_OF", "NODE", lin, "NODE", lin).status_code == 422
    assert edge(client, editor, "HAS_EXAMPLE", "NODE", unk, "NODE", lin).status_code == 422        # a lesson is not an example
    assert edge(client, editor, "PREREQUISITE_OF", "NODE", lin, "NODE", pre).status_code == 422    # would close a cycle
    assert edge(client, editor, "RELATED_TO", "NODE", str(uuid.uuid4()), "NODE", unk).status_code == 404


def test_related_concepts_prerequisites_and_missing_chain(client, editor, reader, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, pre, unk, side, intc = node_ids(client, editor, cid, L_LIN, L_PRE, "المجهول", "طرفا المعادلة", "العدد الصحيح")
    edge(client, editor, "RELATED_TO", "NODE", unk, "NODE", side)
    edge(client, editor, "PREREQUISITE_OF", "NODE", intc, "NODE", unk)
    rel = client.get(f"{API}/graph/nodes/{unk}/related", headers=reader, params={"depth": 2}).json()["related"]
    assert {r["name"] for r in rel} == {"طرفا المعادلة", "العدد الصحيح"} and {r["distance"] for r in rel} == {1}
    path = client.get(f"{API}/graph/nodes/{lin}/prerequisites", headers=reader).json()["learning_path"]
    assert [p["name"] for p in path] == [L_PRE]
    assert [m["name"] for m in client.get(f"{API}/graph/nodes/{lin}/missing-prerequisites", headers=editor).json()["missing"]] == [L_PRE]
    add_asset(db_session, pre, "explanation", lesson=L_PRE)                                      # approved coverage of the prerequisite
    assert client.get(f"{API}/graph/nodes/{lin}/missing-prerequisites", headers=editor).json()["missing"] == []
    assert client.get(f"{API}/graph/nodes/{lin}/missing-prerequisites", headers=reader).status_code == 403


def test_edge_update_delete_are_audited_and_allow_re_adding(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    unk, side = node_ids(client, editor, cid, "المجهول", "طرفا المعادلة")
    eid = edge(client, editor, "RELATED_TO", "NODE", unk, "NODE", side).json()["id"]
    assert client.patch(f"{API}/graph/edges/{eid}", headers=editor, json={"confidence": 0.6}).json()["confidence"] == 0.6
    assert client.delete(f"{API}/graph/edges/{eid}", headers=editor, params={"reason": "wrong link"}).status_code == 204
    assert client.get(f"{API}/graph/edges/{eid}", headers=editor).status_code == 404
    assert edge(client, editor, "RELATED_TO", "NODE", unk, "NODE", side).status_code == 201
    rows = {a.action: a.details for a in db_session.scalars(select(AuditLog).where(AuditLog.entity_type == "graph_edge", AuditLog.entity_id == eid))}
    assert rows["graph.edge_delete"]["reason"] == "wrong link" and rows["graph.edge_update"]["before"]["confidence"] == 1.0


def test_linking_assets_creates_typed_edges_and_sources_are_connected(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    db_session.add(Source(name="textbook", base_url="https://textbook.example", host="textbook.example", policy=SourcePolicy.ALLOWED, authority_score=0.9))
    db_session.commit()
    ex = add_asset(db_session, None, "example")
    exp = add_asset(db_session, None, "explanation")
    for a in (ex, exp):
        assert client.post(f"{API}/graph/link-asset", headers=editor, json={"asset_id": str(a.id), "node_id": lin}).status_code == 201
    edges = client.get(f"{API}/graph/entities/{lin}/edges", headers=editor).json()["edges"]
    assert {e["relation"] for e in edges} >= {"HAS_EXAMPLE", "EXPLAINS", "PREREQUISITE_OF"}
    assert client.get(f"{API}/graph/entities/{exp.id}/edges", headers=editor, params={"relation": "SUPPORTED_BY"}).json()["count"] == 1
    db_session.refresh(ex)
    assert str(ex.curriculum_node_id) == lin


def test_readers_never_see_edges_to_unapproved_content(client, editor, reader, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    draft, ok = add_asset(db_session, None, "example", status="DRAFT"), add_asset(db_session, None, "explanation")
    for a in (draft, ok):
        client.post(f"{API}/graph/link-asset", headers=editor, json={"asset_id": str(a.id), "node_id": lin})
    seen = client.get(f"{API}/graph/entities/{lin}/edges", headers=reader).json()["edges"]
    assert {e["relation"] for e in seen} == {"EXPLAINS", "PREREQUISITE_OF"}                       # no HAS_EXAMPLE to the draft
    assert len(client.get(f"{API}/graph/entities/{lin}/edges", headers=editor).json()["edges"]) == 3
    claim = ExtractedClaim(learning_asset_id=ok.id, claim_text="c", claim_type=ClaimType.OTHER, status=ClaimStatus.VERIFIED)
    db_session.add(claim)
    db_session.commit()
    src = Source(name="s", base_url="https://s.example", host="s.example", policy=SourcePolicy.ALLOWED)
    db_session.add(src)
    db_session.commit()
    assert edge(client, editor, "SUPPORTED_BY", "CLAIM", str(claim.id), "SOURCE", str(src.id)).status_code == 201
    assert client.get(f"{API}/graph/entities/{claim.id}/edges", headers=reader).json()["count"] == 0
