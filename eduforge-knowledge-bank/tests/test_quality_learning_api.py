"""Knowledge Quality and Continuous Learning: API + database."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import (AuditLog, EvidenceRecord, ExtractedClaim, KnowledgeGap, LearningAsset, QualityAssessment, RawDocument, ScanRun,
                        ScanSchedule, Source, SourceSnapshot)
from app.models.enums import ClaimStatus, ClaimType, EvidenceStance, SourcePolicy, TaskStatus
from app.models.ops import ResearchTask
from app.services import continuous_learning as cl
from tests.stage9_helpers import API, L_LIN, add_asset, full_math_lesson, node_ids, seed_curriculum


def claim(db, asset, verdict, status=ClaimStatus.PENDING_VERIFICATION):
    c = ExtractedClaim(learning_asset_id=asset.id, claim_text=f"claim {uuid.uuid4()}", claim_type=ClaimType.OTHER, status=status,
                       meta={"verification_status": verdict})
    db.add(c)
    db.commit()
    return c


# ------------------------------------------------------------------ quality
def test_assessment_has_reasons_and_never_equates_support_with_correctness(client, editor, db_session):
    db_session.add(Source(name="textbook", base_url="https://textbook.example", host="textbook.example", policy=SourcePolicy.ALLOWED, authority_score=0.9))
    db_session.commit()
    a = add_asset(db_session, None, "explanation")
    c = claim(db_session, a, "SOURCE_SUPPORTED")
    db_session.add(EvidenceRecord(claim_id=c.id, source_id=db_session.scalar(select(Source.id)), raw_document_id=None, stance=EvidenceStance.SUPPORTS,
                                  independence_group="g1", authority_score=0.9, relevance_score=0.8, excerpt="x"))
    db_session.commit()
    r = client.get(f"{API}/quality/assets/{a.id}", headers=editor).json()
    assert r["correctness"] == "UNKNOWN" and r["support_score"] > 0.5
    assert all(d["reason"] for d in r["dimensions"].values()) and r["dimensions"]["source_reliability"]["score"] == 0.9
    assert "not the probability" in r["note"]


def test_deterministically_verified_math_and_rejected_content(client, editor, db_session):
    good, bad = add_asset(db_session, None, "exercise"), add_asset(db_session, None, "exercise")
    claim(db_session, good, "MATH_VERIFIED", ClaimStatus.VERIFIED)
    claim(db_session, bad, "MATH_VERIFIED", ClaimStatus.VERIFIED)
    claim(db_session, bad, "REJECTED", ClaimStatus.REJECTED)
    assert client.get(f"{API}/quality/assets/{good.id}", headers=editor).json()["correctness"] == "DETERMINISTICALLY_VERIFIED"
    r = client.get(f"{API}/quality/assets/{bad.id}", headers=editor).json()
    assert r["correctness"] == "REJECTED" and "HAS_REJECTED_CLAIMS" in r["flags"] and r["dimensions"]["verification"]["score"] == 0.0


def test_stale_content_is_flagged_and_history_is_kept(client, editor, db_session):
    old = add_asset(db_session, None, "explanation", age_days=1500)
    r = client.get(f"{API}/quality/assets/{old.id}", headers=editor).json()
    assert "OUTDATED" in r["flags"] and r["dimensions"]["freshness"]["score"] < 0.5
    client.get(f"{API}/quality/assets/{old.id}", headers=editor)
    hist = client.get(f"{API}/quality/assets/{old.id}/history", headers=editor).json()["assessments"]
    assert len(hist) == 2 and hist[0]["rubric_version"]


def test_quality_endpoints_are_editor_only(client, reader, db_session):
    a = add_asset(db_session, None, "explanation")
    assert client.get(f"{API}/quality/assets/{a.id}", headers=reader).status_code == 403


def test_revision_keeps_original_until_admin_approval(client, editor, admin, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    old = add_asset(db_session, lin, "explanation", age_days=900)
    rev = client.post(f"{API}/quality/assets/{old.id}/revisions", headers=editor,
                      json={"content": "نص منقح للشرح بعد مراجعة المصادر الحديثة " * 3, "reason": "source updated"}).json()
    assert rev["status"] == "DRAFT" and rev["version"] == 2
    db_session.refresh(old)
    assert old.status == "APPROVED"                                   # nothing replaced yet
    assert client.post(f"{API}/learning/assets/{rev['revision_asset_id']}/approve", headers=editor).status_code == 403
    assert client.post(f"{API}/learning/assets/{rev['revision_asset_id']}/approve", headers=admin).status_code == 200
    db_session.refresh(old)
    assert old.status == "SUPERSEDED"
    assert db_session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "asset.superseded")) == 1


# ------------------------------------------------------------------ continuous learning
def scan(db, scope=None, actor="t"):
    run = cl.create_scan(db, scope or {}, actor=actor)
    return cl.run_scan(db, run.id)


def test_scan_opens_deduplicated_gaps_and_does_not_invent_research(client, editor, db_session):
    seed_curriculum(client, editor)
    first = scan(db_session)
    assert first.status == TaskStatus.SUCCEEDED and first.stats["gaps_opened"] > 0
    assert any("external research skipped" in line for line in first.log) and first.stats["research_queued"] == 0
    n = db_session.scalar(select(func.count()).select_from(KnowledgeGap))
    second = scan(db_session)
    assert second.stats["gaps_opened"] == 0 and second.stats["gaps_refreshed"] > 0
    assert db_session.scalar(select(func.count()).select_from(KnowledgeGap)) == n
    types = set(db_session.scalars(select(KnowledgeGap.gap_type)))
    assert {"MISSING_LESSON", "MISSING_PREREQUISITE"} <= types
    gaps = client.get(f"{API}/continuous-learning/gaps", headers=editor, params={"gap_type": "missing_lesson"}).json()
    assert len(gaps) == 2 and all(g["status"] == "OPEN" for g in gaps)


def test_research_is_bounded_per_scan_and_per_day(client, editor, db_session, settings, monkeypatch):
    from pydantic import SecretStr
    limited = settings.model_copy(update={"tavily_api_key": SecretStr("test"), "learning_max_research_per_scan": 1,
                                          "learning_max_research_per_day": 2})
    monkeypatch.setattr(cl, "get_settings", lambda: limited)
    seed_curriculum(client, editor)
    counts = []
    for _ in range(3):
        counts.append(scan(db_session).stats["research_queued"])
    assert counts == [1, 1, 0]                                           # per-scan cap, then the daily cap
    tasks = db_session.scalars(select(ResearchTask).where(ResearchTask.idempotency_key.like("gap:%"))).all()
    assert len(tasks) == 2 and all(t.meta["publication_requires_review"] is True for t in tasks)
    assert db_session.scalar(select(func.count()).select_from(KnowledgeGap).where(KnowledgeGap.status == "IN_PROGRESS")) == 2


def test_cancel_retry_failure_logging_and_history(client, editor, db_session, monkeypatch):
    run = cl.create_scan(db_session, {}, actor="t")
    assert client.post(f"{API}/continuous-learning/scans/{run.id}/cancel", headers=editor).json()["status"] == "CANCELLED"
    assert client.post(f"{API}/continuous-learning/scans/{run.id}/cancel", headers=editor).status_code == 409
    retried = client.post(f"{API}/continuous-learning/scans/{run.id}/retry", headers=editor)
    assert retried.status_code == 202 and retried.json()["kind"] == "RETRY" and retried.json()["retry_of_id"] == str(run.id)

    def boom(*a, **k):
        raise RuntimeError("search provider down")
    monkeypatch.setattr(cl, "detect_gaps", boom)
    failing = cl.create_scan(db_session, {}, actor="t")
    states = [cl.run_scan(db_session, failing.id).status for _ in range(3)]
    assert states == [TaskStatus.RETRYING, TaskStatus.RETRYING, TaskStatus.DEAD_LETTER]
    done = client.get(f"{API}/continuous-learning/scans/{failing.id}", headers=editor).json()
    assert done["attempts"] == 3 and "search provider down" in done["last_error"] and any("failed" in l for l in done["log"])
    assert len(client.get(f"{API}/continuous-learning/scans", headers=editor).json()) >= 3     # task history


def test_cancellation_of_a_running_scan_is_honoured_between_phases(db_session, monkeypatch):
    run = cl.create_scan(db_session, {}, actor="t")

    def request_cancel(db, scope, **k):
        row = db.get(ScanRun, run.id)
        row.cancel_requested = True
        db.commit()
        return {"lessons_scanned": 0, "gaps_opened": 0, "gaps_refreshed": 0, "coverage_percentage": 0}
    monkeypatch.setattr(cl, "detect_gaps", request_cancel)
    assert cl.run_scan(db_session, run.id).status == TaskStatus.CANCELLED


def test_schedules_are_managed_by_admins_and_create_one_scan_at_a_time(client, editor, admin, db_session):
    body = {"name": "nightly", "interval_hours": 24, "scope": {"subject": "الرياضيات"}}
    assert client.post(f"{API}/continuous-learning/schedules", headers=editor, json=body).status_code == 403
    sid = client.post(f"{API}/continuous-learning/schedules", headers=admin, json=body).json()["id"]
    assert client.post(f"{API}/continuous-learning/schedules", headers=admin, json=body).status_code == 409
    row = db_session.get(ScanSchedule, uuid.UUID(sid))
    assert cl.run_due_schedules(db_session) == []                         # not due yet
    row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    created = cl.run_due_schedules(db_session)
    assert len(created) == 1 and db_session.get(ScanRun, created[0]).kind == "SCHEDULED"
    row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    assert cl.run_due_schedules(db_session) == []                         # previous scan still active -> skip
    assert client.patch(f"{API}/continuous-learning/schedules/{sid}", headers=admin, json={"enabled": False}).json()["enabled"] is False


def test_outdated_lesson_and_changed_source_become_gaps(client, editor, db_session):
    cid, _ = seed_curriculum(client, editor)
    lin, = node_ids(client, editor, cid, L_LIN)
    assets = full_math_lesson(db_session, lin, age_days=2000)
    src = Source(name="s", base_url="https://s.example", host="s.example", policy=SourcePolicy.ALLOWED)
    db_session.add(src)
    db_session.commit()
    old = SourceSnapshot(source_id=src.id, url="https://s.example/p", content_hash="a" * 64, fetched_at=datetime.now(timezone.utc) - timedelta(days=30))
    new = SourceSnapshot(source_id=src.id, url="https://s.example/p", content_hash="b" * 64, fetched_at=datetime.now(timezone.utc))
    db_session.add_all([old, new])
    db_session.commit()
    doc = RawDocument(title="old copy", content_hash="c" * 64, text_content="x", source_id=src.id, snapshot_id=old.id)
    db_session.add(doc)
    db_session.commit()
    c = claim(db_session, assets[0], "SOURCE_SUPPORTED")
    db_session.add(EvidenceRecord(claim_id=c.id, source_id=src.id, raw_document_id=doc.id, stance=EvidenceStance.SUPPORTS,
                                  independence_group="g", authority_score=0.7, relevance_score=0.7, excerpt="x"))
    claim(db_session, assets[1], "CONFLICTED", ClaimStatus.CONFLICTED)
    db_session.commit()
    run = scan(db_session)
    types = set(db_session.scalars(select(KnowledgeGap.gap_type)))
    assert {"OUTDATED_CONTENT", "SOURCE_CHANGED", "NEW_CONFLICT"} <= types
    assert run.stats["source_changes"] == 1 and run.stats["new_conflicts"] == 1 and run.stats["assets_assessed"] > 0
    assert db_session.scalar(select(func.count()).select_from(QualityAssessment)) > 0


def test_gap_preparation_produces_drafts_only_and_never_resolves_the_gap_by_itself(client, editor, db_session):
    from app.services import preparation_jobs
    cid, _ = seed_curriculum(client, editor)
    scan(db_session)
    gap = db_session.scalar(select(KnowledgeGap).where(KnowledgeGap.gap_type == "MISSING_LESSON", KnowledgeGap.description.contains(L_LIN)))
    r = client.post(f"{API}/continuous-learning/gaps/{gap.id}/prepare", headers=editor)
    assert r.status_code == 202 and r.json()["status"] == "PENDING"
    job_id = uuid.UUID(r.json()["job_id"])
    assert client.post(f"{API}/continuous-learning/gaps/{gap.id}/prepare", headers=editor).json()["deduplicated"] is True
    preparation_jobs.run_job(db_session, job_id)
    assert db_session.get(__import__("app.models", fromlist=["PreparationJob"]).PreparationJob, job_id).status == TaskStatus.SUCCEEDED
    assert db_session.scalar(select(func.count()).select_from(LearningAsset).where(LearningAsset.status == "APPROVED")) == 0
    again = scan(db_session)
    db_session.refresh(gap)
    assert gap.status == "IN_PROGRESS" and again.stats["gaps_resolved"] == 0           # drafts do not close gaps
    assert client.patch(f"{API}/continuous-learning/gaps/{gap.id}", headers=editor, json={"status": "DISMISSED", "note": "duplicate"}).json()["status"] == "DISMISSED"
