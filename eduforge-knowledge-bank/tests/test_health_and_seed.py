from app.api.health import get_redis_client
from tests.conftest import FakeRedis


def test_live(client):
    assert client.get("/health/live").json() == {"status": "ok"}


def test_ready_ok(client):
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["checks"] == {"database": "ok", "redis": "ok"}


def test_ready_503_when_redis_down(fastapi_app, client):
    fastapi_app.dependency_overrides[get_redis_client] = lambda: FakeRedis(fail=True)
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["redis"] == "error" and r.json()["status"] == "degraded"


def test_seed_creates_ten_domains_and_is_idempotent(db_session):
    from app.services.seed import DEFAULT_DOMAINS, seed_default_domains

    assert len(seed_default_domains(db_session)) == len(DEFAULT_DOMAINS) == 10
    assert seed_default_domains(db_session) == []
    from sqlalchemy import select
    from app.models import KnowledgeDomain

    arabic = db_session.scalar(select(KnowledgeDomain).where(KnowledgeDomain.slug == "arabic-language"))
    assert arabic.default_language == "ar"


def test_openapi_lists_only_implemented_groups(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/domains" in paths and "/api/v1/topics" in paths and "/api/v1/sources" in paths
    assert not any(p.startswith("/api/v1/research") or p.startswith("/api/v1/agents") for p in paths)
