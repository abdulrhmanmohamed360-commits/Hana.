"""Integration tests: real FastAPI app + real SQLAlchemy models/services on SQLite (in-memory).
PostgreSQL-only behaviour (FTS, pgvector, Alembic) lives in tests/test_postgres_integration.py.
The only test double is the LLM *transport* (see tests/helpers.py)."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.agents.llm import StructuredLLM
from app.engines.math_verify import verify_solution
from app.models import AuditLog, Embedding, ExtractedClaim, LearningAsset, PreparationJob
from app.models.enums import ClaimStatus, SourcePolicy, TaskStatus
from app.schemas.prepare import PrepareRequest
from app.services import preparation_jobs
from app.services.prepare import PrepareContext, build_package
from app.services.retrieval import retrieve_hybrid
from app.services.verification import verify_text_claim
from tests.helpers import (ANALYSIS_OK, RESEARCH_OK, REVIEW_OK, make_document, make_source, scripted_transport,
                           teacher_json)

URL = "/api/v1/knowledge/prepare"
BASE = {"country": "Egypt", "curriculum": "National Curriculum", "grade": "Third Preparatory", "semester": 1,
        "subject": "الرياضيات", "lesson": "المعادلات الخطية", "language": "ar", "difficulty": "beginner"}
TEXTBOOK = ("المعادلة الخطية هي معادلة من الدرجة الأولى تحتوي على مجهول واحد. لحل المعادلة الخطية ننقل الحدود "
            "المعلومة إلى طرف والمجهول إلى الطرف الآخر ثم نقسم على معامل المجهول.")


def _prepare(client, headers, **over):
    return client.post(URL, json={**BASE, **over}, headers=headers)


# ---------------------------------------------------------------- acceptance workflow (Arabic lesson)
def test_acceptance_arabic_lesson_package(client, editor, db_session):
    make_document(db_session, "كتاب الرياضيات - الصف الثالث الإعدادي", TEXTBOOK, page=14)
    r = _prepare(client, editor)
    assert r.status_code == 200, r.text
    pkg = r.json()
    items = pkg["items"]

    # retrieved existing knowledge, with provenance
    assert items["explanation"][0]["origin"] == "source_passage"
    assert items["explanation"][0]["provenance"][0]["page_start"] == 14
    assert items["definitions"], "definitional sentence should be extracted from the textbook"

    # generated materials exist and are verified deterministically
    assert len(items["solved_examples"]) == 2 and len(items["exercises"]) == 5 and len(items["questions"]) == 3
    assert len(items["answers"]) == 8
    for kind in ("solved_examples", "exercises", "questions"):
        for it in items[kind]:
            assert it["trust"] == "DRAFT" and it["verification_status"] == "MATH_VERIFIED", it
            assert it["claims"] and all(c["method"] == "sympy_deterministic" for c in it["claims"])

    # independent re-check of every model answer, NOT trusting the server's verdict
    for ex in items["exercises"]:
        d = ex["data"]
        assert verify_solution(d["equation"], d["variable"], d["answer"]).status == "CORRECT"

    # honesty: nothing is approved, summary could not be produced without an LLM, and we say why
    assert pkg["contains_unapproved_content"] is True
    assert all(i["trust"] != "APPROVED" for lst in pkg["items"].values() for i in lst)
    assert pkg["status"] == "PARTIAL"
    assert [m["content_type"] for m in pkg["missing"]] == ["summary"]
    assert "async" in pkg["missing"][0]["reason"]
    assert items["sources"], "sources must be compiled from provenance"

    # persisted: drafts, verified claims, audit trail
    assets = db_session.scalars(select(LearningAsset)).all()
    assert {a.asset_type for a in assets} == {"example", "exercise", "quiz"} and all(a.status == "DRAFT" for a in assets)
    claims = db_session.scalars(select(ExtractedClaim)).all()
    assert claims and all(c.learning_asset_id and c.raw_document_id is None for c in claims)
    assert all(c.status == ClaimStatus.VERIFIED for c in claims)
    assert db_session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "prepare.package")) == 1


def test_second_request_reuses_drafts_without_duplicates(client, editor, db_session):
    first = _prepare(client, editor, requested_content=["exercises"]).json()
    n_assets = db_session.scalar(select(func.count()).select_from(LearningAsset))
    n_claims = db_session.scalar(select(func.count()).select_from(ExtractedClaim))
    second = _prepare(client, editor, requested_content=["exercises"]).json()
    assert db_session.scalar(select(func.count()).select_from(LearningAsset)) == n_assets
    assert db_session.scalar(select(func.count()).select_from(ExtractedClaim)) == n_claims
    assert [i["body"] for i in first["items"]["exercises"]] == [i["body"] for i in second["items"]["exercises"]]
    assert all(i["verification_status"] == "MATH_VERIFIED" for i in second["items"]["exercises"])


def test_answers_are_not_leaked_when_not_requested(client, editor):
    pkg = _prepare(client, editor, requested_content=["exercises"]).json()
    for ex in pkg["items"]["exercises"]:
        assert "answer" not in ex["data"] and "steps" not in ex["data"]
        assert "الإجابة" not in ex["body"]


def test_english_lesson_generation(client, editor):
    pkg = _prepare(client, editor, language="en", subject="Mathematics", lesson="Linear Equations",
                   requested_content=["exercises", "answers"]).json()
    assert pkg["status"] == "COMPLETE" and pkg["items"]["exercises"][0]["body"].startswith("Solve")


# ---------------------------------------------------------------- trust boundaries
def test_approved_assets_are_returned_as_approved_and_are_not_regenerated(client, editor, db_session):
    db_session.add(LearningAsset(education_system="Egypt", stage="Preparatory", grade="Third Preparatory",
                                 subject="Mathematics", lesson="Linear equations", asset_type="explanation",
                                 title="شرح", content="شرح معتمد من المراجع", language="ar", status="APPROVED",
                                 reviewed_by="admin", created_by="t", sources=[{"title": "المنهج", "url": "https://x.edu/a"}]))
    db_session.commit()
    pkg = _prepare(client, editor, requested_content=["explanation"]).json()
    it = pkg["items"]["explanation"][0]
    assert (it["trust"], it["verification_status"], it["origin"]) == ("APPROVED", "APPROVED", "approved_asset")
    assert pkg["contains_unapproved_content"] is False and pkg["status"] == "COMPLETE"
    assert db_session.scalar(select(func.count()).select_from(LearningAsset).where(LearningAsset.status == "DRAFT")) == 0


def test_draft_assets_are_never_returned_as_approved(client, editor, reader, db_session):
    _prepare(client, editor, requested_content=["exercises"])
    pack = client.post("/api/v1/learning/pack", headers=reader,
                       json={"education_system": "Egypt", "stage": "unspecified", "grade": "Third Preparatory",
                             "subject": "الرياضيات", "lesson": "المعادلات الخطية", "include": ["exercise"]}).json()
    assert pack["assets_count"] == 0, "generated drafts must not leak through the approved-only pack endpoint"


def test_unreviewed_web_content_stays_unverified(client, editor, db_session):
    make_document(db_session, "web page", TEXTBOOK, doc_meta={"verification_status": "UNVERIFIED_SOURCE_CONTENT"},
                  chunk_meta={"verification_status": "UNVERIFIED"})
    pkg = _prepare(client, editor, requested_content=["explanation"]).json()
    assert pkg["items"]["explanation"][0]["verification_status"] == "UNVERIFIED"
    assert verify_text_claim(db_session, "المعادلة الخطية هي معادلة من الدرجة الأولى تحتوي على مجهول واحد").status == "UNVERIFIED"


def test_source_not_allowlisted_cannot_support_a_claim(db_session):
    src = make_source(db_session, "pending.example.edu", SourcePolicy.PENDING)
    make_document(db_session, "pending source", TEXTBOOK, source=src)
    assert verify_text_claim(db_session, "المعادلة الخطية هي معادلة من الدرجة الأولى تحتوي على مجهول واحد").status == "UNVERIFIED"
    ok = make_source(db_session, "ok.example.edu", SourcePolicy.ALLOWED)
    make_document(db_session, "allowed source", TEXTBOOK + " نص إضافي مختلف.", source=ok)
    assert verify_text_claim(db_session, "المعادلة الخطية هي معادلة من الدرجة الأولى تحتوي على مجهول واحد").status == "SOURCE_SUPPORTED"


# ---------------------------------------------------------------- conflicts
def test_conflicting_sources_are_detected_in_the_package(client, editor, db_session):
    make_document(db_session, "كتاب أ", "مجموع زوايا المثلث يساوي 180 درجة في الهندسة المستوية.")
    make_document(db_session, "كتاب ب", "مجموع زوايا المثلث يساوي 360 درجة في الهندسة المستوية.")
    pkg = _prepare(client, editor, lesson="زوايا المثلث", requested_content=["explanation"]).json()
    assert pkg["conflicts"] and pkg["conflicts"][0]["kind"] == "NUMERIC"
    assert {pkg["conflicts"][0]["source_a"], pkg["conflicts"][0]["source_b"]} == {"كتاب أ", "كتاب ب"}


def test_generated_claim_contradicting_a_source_is_conflicted(db_session):
    make_document(db_session, "كتاب أ", "مجموع زوايا المثلث يساوي 180 درجة في الهندسة المستوية.")
    verdict = verify_text_claim(db_session, "مجموع زوايا المثلث يساوي 200 درجة في الهندسة المستوية.")
    assert verdict.status == "CONFLICTED"


# ---------------------------------------------------------------- async jobs + agents
def _teacher_with_bad_math() -> str:
    good = {"title": "مثال صحيح", "problem": "2س + 3 = 7", "variable": "س", "answer": "2",
            "steps": [{"equation": "2س = 4", "explanation": "نطرح 3"}, {"equation": "س = 2", "explanation": "نقسم على 2"}]}
    bad = {**good, "title": "مثال خاطئ", "answer": "3",
           "steps": [{"equation": "2س = 4", "explanation": "نطرح 3"}, {"equation": "س = 3", "explanation": "خطأ"}]}
    return teacher_json(
        explanation="الكتابة بالقلم الرصاص ضرورية دائما للطلاب في المدرسة.",
        definitions=[{"term": "المعادلات التربيعية", "definition": "هي معادلات من الدرجة الثانية",
                      "evidence_ids": ["E1", "E99"]}],
        summary="ملخص", evidence_ids_used=["E1", "E42"], solved_examples=[good, bad])


QUAD = {"lesson": "المعادلات التربيعية", "mode": "async",
        "requested_content": ["explanation", "definitions", "solved_examples", "summary"]}


def _llm(teacher: str | list[str]):
    transport, calls = scripted_transport({"Research Agent": RESEARCH_OK, "Analysis Agent": ANALYSIS_OK,
                                           "Teacher Agent": teacher, "Verification Agent": REVIEW_OK})
    return StructuredLLM(transport, "test-model"), calls


def test_async_job_lifecycle_agents_and_verification(client, editor, db_session, engine, fastapi_app):
    make_document(db_session, "كتاب", "المعادلات التربيعية هي معادلات من الدرجة الثانية. يمكن حلها بالتحليل.")
    r = _prepare(client, editor, **QUAD)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert fastapi_app.state.dispatched == [job_id]
    again = _prepare(client, editor, **QUAD).json()
    assert again["job_id"] == job_id and again["deduplicated"] is True and fastapi_app.state.dispatched == [job_id]
    assert client.get(f"/api/v1/knowledge/jobs/{job_id}", headers=editor).json()["status"] == "PENDING"

    # a *separate* session plays the worker process; state must come from the database, not memory
    worker_session = sessionmaker(bind=engine, expire_on_commit=False)()
    llm, calls = _llm(_teacher_with_bad_math())
    import uuid
    preparation_jobs.run_job(worker_session, uuid.UUID(job_id), llm=llm)
    worker_session.close()
    assert [c["role"] for c in calls] == ["Research Agent", "Analysis Agent", "Teacher Agent", "Verification Agent"]

    job = client.get(f"/api/v1/knowledge/jobs/{job_id}", headers=editor).json()
    assert job["status"] == "SUCCEEDED" and job["progress"]["percent"] == 100 and job["attempts"] == 1
    pkg = job["result"]
    ex = pkg["items"]["solved_examples"]
    assert len(ex) == 1 and ex[0]["verification_status"] == "MATH_VERIFIED"
    assert len(pkg["rejected"]) == 1 and pkg["rejected"][0]["title"] == "مثال خاطئ"
    assert pkg["items"]["explanation"][0]["verification_status"] == "UNVERIFIED"      # unrelated to any source
    assert pkg["items"]["definitions"][0]["verification_status"] == "SOURCE_SUPPORTED"
    assert pkg["items"]["summary"][0]["trust"] == "DRAFT"
    assert any("unknown evidence ids" in n for n in pkg["advisory_notes"])              # fabricated citations dropped
    assert any("لا علاقة" in n for n in pkg["advisory_notes"])                          # advisory review surfaced
    assert pkg["contains_unapproved_content"] is True
    rejected_claims = db_session.scalars(select(ExtractedClaim).where(ExtractedClaim.status == ClaimStatus.REJECTED)).all()
    assert rejected_claims, "rejected claims must stay in the audit trail"


def test_agent_output_is_schema_validated_with_one_repair_retry(db_session):
    make_document(db_session, "كتاب", "المعادلات التربيعية هي معادلات من الدرجة الثانية.")
    llm, calls = _llm(["this is not json", teacher_json(summary="ملخص قصير للدرس")])
    req = PrepareRequest(**{**BASE, **QUAD})
    pkg = build_package(db_session, req, PrepareContext(actor="t", llm=llm, allow_llm=True))
    assert [c["turns"] for c in calls if c["role"] == "Teacher Agent"] == [2, 4]   # second call carries the error
    assert pkg.items["summary"]


def test_invalid_agent_output_becomes_an_honest_gap_not_fake_content(db_session):
    make_document(db_session, "كتاب", "المعادلات التربيعية هي معادلات من الدرجة الثانية.")
    llm, _ = _llm("{broken")
    pkg = build_package(db_session, PrepareRequest(**{**BASE, **QUAD}), PrepareContext(actor="t", llm=llm, allow_llm=True))
    assert any("agent pipeline failed" in m.reason for m in pkg.missing)
    assert pkg.items["solved_examples"] == []


def test_failed_job_retries_then_dead_letters(db_session, monkeypatch):
    req = PrepareRequest(**{**BASE, "mode": "async"})
    job, _ = preparation_jobs.create_or_get_job(db_session, req, "editor")
    monkeypatch.setattr(preparation_jobs, "build_package", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    for expected in (TaskStatus.RETRYING, TaskStatus.RETRYING, TaskStatus.DEAD_LETTER):
        preparation_jobs.run_job(db_session, job.id)
        assert db_session.get(PreparationJob, job.id).status == expected
    final = db_session.get(PreparationJob, job.id)
    assert final.attempts == 3 and "boom" in final.last_error
    preparation_jobs.run_job(db_session, job.id)           # dead-lettered jobs are never claimed again
    assert db_session.get(PreparationJob, job.id).attempts == 3


def test_job_claim_is_exclusive_and_stale_jobs_recover(db_session):
    from datetime import datetime, timedelta, timezone
    job, _ = preparation_jobs.create_or_get_job(db_session, PrepareRequest(**{**BASE, "mode": "async"}), "editor")
    job.status = TaskStatus.RUNNING
    job.started_at = datetime.now(timezone.utc)
    db_session.commit()
    assert preparation_jobs.run_job(db_session, job.id).status == TaskStatus.RUNNING   # someone else owns it
    job.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.commit()
    assert preparation_jobs.requeue_stale(db_session) == 1
    assert db_session.get(PreparationJob, job.id).status == TaskStatus.RETRYING
    assert job.id in preparation_jobs.pending_job_ids(db_session)


# ---------------------------------------------------------------- API contract
def test_prepare_requires_editor_and_validates_input(client, reader):
    assert client.post(URL, json=BASE).status_code == 401
    assert client.post(URL, json=BASE, headers=reader).status_code == 403


def test_sync_research_is_rejected(client, editor):
    assert _prepare(client, editor, allow_research=True).status_code == 422


def test_unknown_job_is_404(client, editor):
    import uuid
    assert client.get(f"/api/v1/knowledge/jobs/{uuid.uuid4()}", headers=editor).status_code == 404


# ---------------------------------------------------------------- hybrid retrieval on the database
def test_arabic_normalization_matches_diacritized_queries(db_session):
    make_document(db_session, "كتاب", "المعادلة الخطية تحتوي على مجهول واحد.")
    hits = retrieve_hybrid(db_session, "المُعَادَلَةُ الخَطِّيَّةُ").hits
    assert len(hits) == 1


def test_english_and_arabic_retrieval(db_session):
    make_document(db_session, "en", "A linear equation has one unknown variable.")
    make_document(db_session, "ar", "المعادلة الخطية تحتوي على مجهول واحد.")
    assert retrieve_hybrid(db_session, "linear equation unknown").hits[0].document.title == "en"
    assert retrieve_hybrid(db_session, "المعادلة الخطية مجهول").hits[0].document.title == "ar"


def test_vector_signal_is_fused_with_lexical(db_session):
    _, lexical_only = make_document(db_session, "lex", "المعادلة الخطية في الجبر")
    _, semantic_only = make_document(db_session, "sem", "نص مختلف تماما عن الموضوع")
    _, both = make_document(db_session, "both", "المعادلة الخطية وحل المجهول")
    for chunk, vec in ((lexical_only, [0.0, 1.0, 0.0]), (semantic_only, [1.0, 0.0, 0.0]), (both, [0.9, 0.1, 0.0])):
        db_session.add(Embedding(owner_type="knowledge_chunk", owner_id=chunk.id, model="m", dimensions=3, embedding=vec))
    db_session.commit()
    res = retrieve_hybrid(db_session, "المعادلة الخطية", query_embedding=[1.0, 0.0, 0.0], embedding_model="m")
    assert set(res.signals_used) == {"lexical", "vector"} and res.mode == "hybrid_rrf"
    titles = [h.document.title for h in res.hits]
    assert titles[0] in {"both", "lex"} and "sem" in titles   # "sem" is found ONLY by the semantic signal
    assert res.hits[0].signals.keys() == {"lexical", "vector"}
    assert "fts" in res.signals_unavailable               # SQLite: reported honestly, not faked


def test_retrieve_endpoint_reports_signals(client, reader, db_session):
    make_document(db_session, "كتاب", "المعادلة الخطية تحتوي على مجهول واحد.")
    body = client.post("/api/v1/knowledge/retrieve", json={"query": "المعادلة الخطية"}, headers=reader).json()
    assert body["count"] == 1 and body["signals_used"] == ["lexical"] and "vector" in body["signals_unavailable"]


def test_growth_task_status_endpoint(client, editor, db_session):
    from app.models.ops import ResearchTask
    t = ResearchTask(goal="g", created_by="t")
    db_session.add(t)
    db_session.commit()
    body = client.get(f"/api/v1/growth/tasks/{t.id}", headers=editor).json()
    assert body["status"] == "PENDING" and body["results_are_unverified"] is True
