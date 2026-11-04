"""One workflow through all four Stage 9 systems plus the Stage 8 preparation workflow (API + database, SQLite).

Curriculum Intelligence -> /knowledge/prepare -> Knowledge Graph links -> Coverage -> Quality -> Continuous Learning,
with the acceptance property throughout: nothing generated is ever treated as approved until an admin approves it."""
from sqlalchemy import select

from app.models import KnowledgeGap, LearningAsset
from app.services import continuous_learning as cl
from tests.helpers import make_document
from tests.stage9_helpers import API, GRADE, L_LIN, L_PRE, SUBJECT, add_asset, node_ids, seed_curriculum

TEXTBOOK = ("المعادلة الخطية هي معادلة من الدرجة الأولى تحتوي على مجهول واحد. لحل المعادلة الخطية ننقل الحدود "
            "المعلومة إلى طرف والمجهول إلى الطرف الآخر ثم نقسم على معامل المجهول.")


def lesson_row(client, headers, node_id):
    return client.get(f"{API}/coverage/lessons/{node_id}", headers=headers).json()


def test_curriculum_coverage_graph_quality_and_learning_work_as_one_system(client, editor, admin, db_session):
    # 1. Curriculum Intelligence provides the structure
    cid, _ = seed_curriculum(client, editor)
    lin, pre = node_ids(client, editor, cid, L_LIN, L_PRE)
    make_document(db_session, "كتاب الرياضيات - الصف الثالث الإعدادي", TEXTBOOK, page=14)
    assert lesson_row(client, editor, lin)["status"] == "MISSING"

    # 2. Prepare: context resolved from the curriculum, drafts verified, linked to the lesson, graph edges created
    r = client.post(f"{API}/knowledge/prepare", headers=editor, json={
        "country": "Egypt", "curriculum": "Egyptian National Curriculum", "grade": GRADE, "semester": 1, "subject": SUBJECT,
        "lesson": L_LIN, "language": "ar", "difficulty": "beginner"})
    assert r.status_code == 200, r.text
    pkg = r.json()
    assert pkg["curriculum"]["status"] == "RESOLVED" and pkg["curriculum"]["node_id"] == lin
    assert [p["name"] for p in pkg["curriculum"]["prerequisites"]] == [L_PRE]
    assert [m["name"] for m in pkg["curriculum"]["missing_prerequisites"]] == [L_PRE]
    assert pkg["curriculum"]["learning_objectives"] == ["حل معادلة خطية بمجهول واحد"]
    assert pkg["items"]["solved_examples"] and all(i["verification_status"] == "MATH_VERIFIED" for i in pkg["items"]["exercises"])
    assert pkg["contains_unapproved_content"] is True

    drafts = db_session.scalars(select(LearningAsset).where(LearningAsset.status == "DRAFT")).all()
    assert {a.asset_type for a in drafts} == {"example", "exercise", "quiz"} and all(str(a.curriculum_node_id) == lin for a in drafts)
    relations = {e["relation"] for e in client.get(f"{API}/graph/entities/{lin}/edges", headers=editor).json()["edges"]}
    assert {"HAS_EXAMPLE", "HAS_QUESTION", "PREREQUISITE_OF"} <= relations

    # 3. Coverage: the knowledge state returned by prepare agrees with the report; drafts are pending, not covered
    st = pkg["knowledge_state"]
    assert st["status"] == "INCOMPLETE" and st["met"] == [] and set(st["pending_review"]) == {"example", "exercise", "quiz"}
    assert st["status"] == lesson_row(client, editor, lin)["status"]
    assert client.get(f"{API}/coverage/report", headers=editor).json()["totals"]["covered_lessons"] == 0

    # 4. Quality: deterministic math is verified, but a human still has to review it
    q = client.get(f"{API}/quality/lessons/{lin}", headers=editor).json()
    assert q["correctness"] == "DETERMINISTICALLY_VERIFIED" and q["needs_human_review"] is True and q["assets_assessed"] == 3

    # 5. Continuous learning: gaps for what is missing, including the prerequisite lesson; no research without a provider
    run = client.post(f"{API}/continuous-learning/scans", headers=editor, json={"subject": SUBJECT})
    assert run.status_code == 202
    import uuid
    done = cl.run_scan(db_session, uuid.UUID(run.json()["id"]))
    assert done.status.value == "SUCCEEDED" and done.stats["research_queued"] == 0
    types = {(g.gap_type, str(g.node_id)) for g in db_session.scalars(select(KnowledgeGap))}
    assert ("MISSING_LESSON", pre) in types and ("MISSING_PREREQUISITE", pre) in types
    assert any(t == "NEEDS_REVIEW" and n == lin for t, n in types) and any(t == "INCOMPLETE_CONTENT" and n == lin for t, n in types)

    # 6. Human review: admin approves the drafts -> counted; the rest of the lesson is supplied and approved
    for a in drafts:
        assert client.post(f"{API}/learning/assets/{a.id}/approve", headers=admin).status_code == 200
    mid = lesson_row(client, editor, lin)
    assert set(mid["met"]) == {"example", "exercise", "quiz"} and mid["status"] == "INCOMPLETE"
    for t, n in (("explanation", 400), ("definition", 80), ("summary", 120)):
        add_asset(db_session, lin, t, chars=n)
    final = lesson_row(client, editor, lin)
    assert final["status"] == "COMPLETE" and final["score"] == 1.0
    assert client.get(f"{API}/coverage/report", headers=editor).json()["totals"]["coverage_percentage"] == 50.0

    # 7. Re-scan: lesson gaps close because approved content now exists; the prerequisite gap stays open
    again = cl.run_scan(db_session, cl.create_scan(db_session, {"subject": SUBJECT}, actor="t").id)
    assert again.stats["gaps_resolved"] >= 2
    open_now = {(g.gap_type, str(g.node_id)) for g in db_session.scalars(select(KnowledgeGap).where(KnowledgeGap.status.in_(("OPEN", "IN_PROGRESS"))))}
    assert ("MISSING_PREREQUISITE", pre) in open_now and not any(n == lin and t in ("INCOMPLETE_CONTENT", "NEEDS_REVIEW") for t, n in open_now)
    assert [m["name"] for m in client.get(f"{API}/graph/nodes/{lin}/missing-prerequisites", headers=editor).json()["missing"]] == [L_PRE]
