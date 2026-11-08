from app.models.enums import TaskStatus
from app.models.learning import LearningAsset
from app.services import growth


def _asset(db, kind):
    row = LearningAsset(
        education_system="Egypt", stage="Preparatory", grade="Third", subject="Mathematics",
        unit="Algebra", lesson="Linear equations", asset_type=kind, title=f"{kind} - linear equations",
        content="Approved educational content for testing.", language="ar", status="APPROVED",
        quality_score=0.9, sources=[{"url": "https://example.edu/source"}],
        learning_objectives=[], tags=[], created_by="test",
    )
    db.add(row)
    db.commit()
    return row


def test_gap_detector_reports_missing_learning_assets(db_session):
    _asset(db_session, "explanation")
    gaps = growth.find_knowledge_gaps(db_session)
    assert len(gaps) == 1
    assert gaps[0]["subject"] == "Mathematics"
    assert "quiz" in gaps[0]["missing_types"]
    assert "explanation" not in gaps[0]["missing_types"]


def test_growth_scan_creates_idempotent_research_task(db_session):
    _asset(db_session, "explanation")
    first = growth.create_research_tasks(db_session, actor="editor")
    second = growth.create_research_tasks(db_session, actor="editor")
    assert len(first) == 1
    assert second == []
    assert first[0].status == TaskStatus.PENDING
    assert first[0].meta["publication_requires_review"] is True


def test_external_search_results_are_marked_unverified(monkeypatch):
    from app.core.config import Settings, get_settings
    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"results": [{"title": "Lesson source", "url": "https://example.edu/lesson",
                                 "content": "A short educational source snippet.", "score": 0.91}]}
    monkeypatch.setattr(growth, "get_settings", lambda: Settings(_env_file=None, tavily_api_key="test-key"))
    monkeypatch.setattr(growth.httpx, "post", lambda *args, **kwargs: FakeResponse())
    results = growth.search_educational_sources("linear equations education")
    assert len(results) == 1
    assert results[0]["verification_status"] == "UNVERIFIED_LEAD"


def test_research_fetch_requires_explicitly_allowed_source(db_session, monkeypatch):
    from types import SimpleNamespace
    from app.models.source import Source
    from app.models.enums import SourcePolicy

    monkeypatch.setattr(growth, "validate_url", lambda *a, **k: SimpleNamespace(host="example.edu"))
    result = growth._fetch_registered_page(db_session, "https://example.edu/lesson", "Lesson", "task-1")
    assert result["ingest_status"] == "NOT_ALLOWLISTED"

    source = Source(name="Example Education", base_url="https://example.edu", host="example.edu",
                    policy=SourcePolicy.ALLOWED, language="en")
    db_session.add(source)
    db_session.commit()

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8", "etag": "v1"}
        content = b"<html><head><title>Lesson</title></head><body>" + (b"Educational concept and explanation. " * 20) + b"</body></html>"
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def raise_for_status(self): return None
        def iter_bytes(self): yield self.content
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def stream(self, *a, **k): return FakeResponse()

    monkeypatch.setattr(growth.httpx, "Client", FakeClient)
    result = growth._fetch_registered_page(db_session, "https://example.edu/lesson", "Lesson", "task-1")
    assert result["ingest_status"] == "INGESTED_UNVERIFIED"
    assert result["chunks_created"] >= 1
    assert result["document_id"]
    assert result["snapshot_id"]


def test_research_fetch_blocks_redirects(db_session, monkeypatch):
    from types import SimpleNamespace
    from app.models.source import Source
    from app.models.enums import SourcePolicy

    source = Source(name="Example Education", base_url="https://example.edu", host="example.edu",
                    policy=SourcePolicy.ALLOWED, language="en")
    db_session.add(source)
    db_session.commit()
    monkeypatch.setattr(growth, "validate_url", lambda *a, **k: SimpleNamespace(host="example.edu"))

    class RedirectResponse:
        status_code = 302
        headers = {"location": "https://other.example/"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def raise_for_status(self): return None
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def stream(self, *a, **k): return RedirectResponse()

    monkeypatch.setattr(growth.httpx, "Client", FakeClient)
    result = growth._fetch_registered_page(db_session, "https://example.edu/lesson", "Lesson", "task-2")
    assert result["ingest_status"] == "REDIRECT_BLOCKED"
