"""Test fixtures. Unit/API tests run on in-memory SQLite (fast, no services needed).
PostgreSQL-specific behaviour (pgvector, JSONB, Alembic) is verified via docker compose, see README."""
from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.deps import get_embedder, get_job_dispatcher, get_llm, get_scan_dispatcher, get_rate_limiter
from app.api.health import get_redis_client
from app.core.config import Settings, get_settings
from app.core.rate_limit import MemoryRateLimiter
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app

ADMIN_KEY, EDITOR_KEY, READER_KEY = "test-admin-key", "test-editor-key", "test-reader-key"


class FakeRedis:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def ping(self):
        if self.fail:
            raise ConnectionError("redis down")
        return True


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture()
def db_session(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None, app_env="test", admin_api_key=ADMIN_KEY, editor_api_key=EDITOR_KEY, reader_api_key=READER_KEY,
        rate_limit_backend="memory", allow_private_networks=False,
    )


@pytest.fixture()
def fastapi_app(session_factory, settings):
    application = create_app()

    def _db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    application.dependency_overrides[get_db] = _db
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_rate_limiter] = lambda: MemoryRateLimiter(10_000, 60)
    application.dependency_overrides[get_redis_client] = lambda: FakeRedis()
    # No embedding provider / LLM in tests unless a test overrides these; jobs are recorded, not sent to a broker.
    application.dependency_overrides[get_embedder] = lambda: None
    application.dependency_overrides[get_llm] = lambda: None
    application.state.dispatched = []
    application.dependency_overrides[get_job_dispatcher] = lambda: application.state.dispatched.append
    application.state.scans_dispatched = []
    application.dependency_overrides[get_scan_dispatcher] = lambda: application.state.scans_dispatched.append
    return application


@pytest.fixture()
def client(fastapi_app):
    return TestClient(fastapi_app)


@pytest.fixture()
def admin():
    return {"X-API-Key": ADMIN_KEY}


@pytest.fixture()
def editor():
    return {"X-API-Key": EDITOR_KEY}


@pytest.fixture()
def reader():
    return {"X-API-Key": READER_KEY}


@pytest.fixture()
def make_domain(client, editor):
    def _make(slug="mathematics", name="Mathematics", **extra):
        r = client.post("/api/v1/domains", json={"slug": slug, "name": name, **extra}, headers=editor)
        assert r.status_code == 201, r.text
        return r.json()

    return _make
