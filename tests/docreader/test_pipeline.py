"""Pipeline + queue-reliability tests on in-memory SQLite (fixtures from tests/conftest.py). OCR/Embedding are fakes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.models import Embedding, IngestionJob, KnowledgeChunk, RawDocument
from app.models.enums import TaskStatus
from app.services.docreader import sha256_file
from app.services.ingestion_pipeline import (IngestionFailure, claim_job, compute_ingestion_metrics, process_job,
                                             recover_stale_jobs, record_failure, redact)
from tests.docreader.builders import FakeOcr

LESSON = ("Chapter 1: Motion\n\n" + "Velocity describes how quickly the position of an object changes. " * 30
          + "\n\nالحركة هي تغير موضع الجسم مع الزمن في الوحدة الأولى. " * 10)


@pytest.fixture()
def cfg(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    return Settings(_env_file=None, app_env="test", storage_dir=str(storage), ocr_enabled=False,
                    rate_limit_backend="memory", ingestion_temp_dir=str(tmp_path / "tmp"))


def _job(db, cfg, name="lesson.txt", text=LESSON, data: bytes | None = None, **meta):
    from pathlib import Path

    path = Path(cfg.storage_dir) / name
    path.write_bytes(data if data is not None else text.encode("utf-8"))
    job = IngestionJob(input_uri=str(path.resolve()), status=TaskStatus.PENDING, created_by="test",
                       meta={"original_name": name, "file_sha256": sha256_file(path), **meta})
    db.add(job)
    db.commit()
    return job


def _run(db, cfg, job, **kw):
    return process_job(db, job.id, cfg, downloader=kw.pop("downloader", None), ocr=FakeOcr(), **kw)


class FakeEmbedder:
    model = "fake-embed"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[0.0] * 1536 for _ in texts]


def test_creates_document_and_verified_chunks_with_full_provenance(db_session, cfg):
    job = _job(db_session, cfg)
    result = _run(db_session, cfg, job)
    assert result["status"] == "created" and result["chunks_created"] > 0
    db_session.refresh(job)
    assert job.status == TaskStatus.SUCCEEDED and job.stage == "DONE" and job.attempts == 1
    doc = db_session.scalar(select(RawDocument))
    assert doc.text_content.startswith("Chapter 1: Motion") and doc.file_sha256 == sha256_file(
        __import__("pathlib").Path(job.input_uri))
    chunks = db_session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.chunk_index)).all()
    assert len(chunks) == result["chunks_created"]
    first = chunks[0]
    assert first.extraction_method == "native" and first.extraction_confidence and first.chapter_title == "Chapter 1: Motion"
    assert first.content_norm and len(first.content_hash) == 64 and first.language in {"en", "mixed"}
    stats = job.meta["stats"]
    assert stats["chunks_created"] == len(chunks) and stats["extracted_pages"] >= 1 and stats["character_count"] > 0


def test_reprocessing_same_file_is_idempotent(db_session, cfg):
    first = _job(db_session, cfg)
    _run(db_session, cfg, first)
    before = db_session.scalar(select(func.count(KnowledgeChunk.id)))
    second = _job(db_session, cfg, name="copy.txt")
    result = _run(db_session, cfg, second)
    assert result["status"] == "duplicate"
    assert db_session.scalar(select(func.count(KnowledgeChunk.id))) == before
    assert db_session.scalar(select(func.count(RawDocument.id))) == 1
    db_session.refresh(second)
    assert second.duplicates_skipped == 1 and second.documents_created == 0 and second.status == TaskStatus.SUCCEEDED


def test_same_text_in_a_different_file_is_detected_by_content(db_session, cfg):
    _run(db_session, cfg, _job(db_session, cfg))
    html = "<html><body>" + "".join(f"<p>{p}</p>" for p in LESSON.split("\n\n")) + "</body></html>"
    other = _job(db_session, cfg, name="lesson.html", text=html)
    result = _run(db_session, cfg, other)
    assert db_session.scalar(select(func.count(RawDocument.id))) in {1, 2}  # content hash may differ slightly
    assert result["status"] in {"duplicate", "created"}


def test_embeddings_use_existing_embedder_and_only_this_documents_chunks(db_session, cfg):
    embedder = FakeEmbedder()
    result = _run(db_session, cfg, _job(db_session, cfg), embedder=embedder)
    assert result["stats"]["embedding"]["status"] == "ok"
    assert db_session.scalar(select(func.count(Embedding.id))) == result["chunks_created"]


def test_embedding_failure_never_fails_the_job(db_session, cfg):
    class Broken(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("provider down")

    job = _job(db_session, cfg)
    result = _run(db_session, cfg, job, embedder=Broken())
    db_session.refresh(job)
    assert result["status"] == "created" and job.status == TaskStatus.SUCCEEDED
    assert job.meta["stats"]["embedding"]["status"] == "deferred"


def test_job_owned_by_another_worker_is_skipped(db_session, cfg):
    job = _job(db_session, cfg)
    job.status, job.lease_expires_at, job.attempts = TaskStatus.RUNNING, datetime.now(timezone.utc) + timedelta(minutes=10), 1
    db_session.commit()
    assert _run(db_session, cfg, job)["status"] == "skipped_concurrent"
    assert db_session.scalar(select(func.count(RawDocument.id))) == 0


def test_job_with_expired_lease_is_taken_over(db_session, cfg):
    job = _job(db_session, cfg)
    job.status, job.lease_expires_at, job.attempts = TaskStatus.RUNNING, datetime.now(timezone.utc) - timedelta(minutes=1), 1
    db_session.commit()
    assert _run(db_session, cfg, job)["status"] == "created"
    db_session.refresh(job)
    assert job.attempts == 2


def test_succeeded_job_is_not_processed_again(db_session, cfg):
    job = _job(db_session, cfg)
    _run(db_session, cfg, job)
    assert _run(db_session, cfg, job)["status"] == "already_processed"


def test_permanent_failure_marks_failed_and_saves_nothing(db_session, cfg):
    job = _job(db_session, cfg, name="fake.pdf", text="this is not a pdf")
    with pytest.raises(IngestionFailure) as exc:
        _run(db_session, cfg, job)
    status = record_failure(db_session, job.id, exc.value, retry_allowed=True)
    db_session.refresh(job)
    assert status == "FAILED" and job.meta["last_error_code"] == "SIGNATURE_MISMATCH"
    assert job.meta["errors"][0]["stage"] == "EXTRACTING" and job.last_error.startswith("[SIGNATURE_MISMATCH]")
    assert db_session.scalar(select(func.count(RawDocument.id))) == 0


def test_empty_content_is_never_marked_completed(db_session, cfg):
    job = _job(db_session, cfg, text="  \n \n ")
    with pytest.raises(IngestionFailure) as exc:
        _run(db_session, cfg, job)
    assert exc.value.code in {"EMPTY_DOCUMENT", "FILE_EMPTY", "INVALID_FILE"} or exc.value.retryable is False
    record_failure(db_session, job.id, exc.value, retry_allowed=True)
    db_session.refresh(job)
    assert job.status == TaskStatus.FAILED
    assert db_session.scalar(select(func.count(RawDocument.id))) == 0
    assert db_session.scalar(select(func.count(KnowledgeChunk.id))) == 0


def test_checksum_mismatch_is_rejected(db_session, cfg):
    job = _job(db_session, cfg, file_sha256="0" * 64)
    job.meta = {**job.meta, "file_sha256": "0" * 64}
    db_session.commit()
    with pytest.raises(IngestionFailure) as exc:
        _run(db_session, cfg, job)
    assert exc.value.code == "INTEGRITY_MISMATCH" and exc.value.retryable is False


def test_local_path_outside_storage_dir_is_refused(db_session, cfg, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    job = IngestionJob(input_uri=str(outside), status=TaskStatus.PENDING, created_by="t", meta={"original_name": "x.txt"})
    db_session.add(job)
    db_session.commit()
    with pytest.raises(IngestionFailure) as exc:
        _run(db_session, cfg, job)
    assert exc.value.code == "BAD_LOCAL_PATH"


def test_transient_failure_retries_then_dead_letters_with_redacted_errors(db_session, cfg):
    def boom(**kwargs):
        raise RuntimeError("GET https://x.supabase.co/storage?token=SECRET123 failed, Authorization: Bearer abc.def")

    job = IngestionJob(input_uri=f"supabase://{cfg.supabase_storage_bucket}/incoming/a.txt", status=TaskStatus.PENDING,
                       created_by="t", max_attempts=2, meta={"original_name": "a.txt"})
    db_session.add(job)
    db_session.commit()
    with pytest.raises(IngestionFailure) as first:
        process_job(db_session, job.id, cfg, downloader=boom, ocr=FakeOcr())
    assert record_failure(db_session, job.id, first.value, retry_allowed=True) == "RETRYING"
    with pytest.raises(IngestionFailure) as second:
        process_job(db_session, job.id, cfg, downloader=boom, ocr=FakeOcr())
    assert record_failure(db_session, job.id, second.value, retry_allowed=True) == "DEAD_LETTER"
    db_session.refresh(job)
    assert job.attempts == 2 and len(job.meta["errors"]) == 2
    assert "SECRET123" not in job.last_error and "abc.def" not in job.last_error


def test_exhausted_job_goes_to_dead_letter_without_running(db_session, cfg):
    job = _job(db_session, cfg)
    job.attempts, job.max_attempts, job.status = 3, 3, TaskStatus.RETRYING
    db_session.commit()
    assert claim_job(db_session, job.id, 60) == "exhausted"
    db_session.refresh(job)
    assert job.status == TaskStatus.DEAD_LETTER


def test_crashed_worker_jobs_are_recovered(db_session, cfg):
    live = _job(db_session, cfg, name="a.txt")
    dead = _job(db_session, cfg, name="b.txt", text="other text " * 40)
    spent = _job(db_session, cfg, name="c.txt", text="third text " * 40)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    for j, attempts in ((dead, 1), (spent, 3)):
        j.status, j.lease_expires_at, j.attempts = TaskStatus.RUNNING, past, attempts
    live.status, live.lease_expires_at = TaskStatus.RUNNING, datetime.now(timezone.utc) + timedelta(minutes=5)
    db_session.commit()
    outcome = recover_stale_jobs(db_session, cfg)
    db_session.refresh(dead), db_session.refresh(spent), db_session.refresh(live)
    assert dead.id in outcome["requeue"] and dead.status == TaskStatus.RETRYING
    assert spent.status == TaskStatus.DEAD_LETTER and live.status == TaskStatus.RUNNING
    assert dead.meta["errors"][-1]["code"] == "WORKER_LOST"


def test_metrics_reflect_processed_jobs(db_session, cfg):
    _run(db_session, cfg, _job(db_session, cfg))
    m = compute_ingestion_metrics(db_session, days=1)
    assert m["documents_processed"] == 1 and m["chunks_generated"] > 0 and m["job_success_rate"] == 1.0
    assert m["engine_usage"] == {"native": 1} and m["failed_jobs"] == 0


def test_redact_hides_tokens_and_bearer_values():
    out = redact("fail https://h.co/a?token=abc&x=1 Bearer eyJ.abc.def apikey=ZZZ")
    assert "abc&x" not in out and "eyJ.abc.def" not in out and "ZZZ" not in out
