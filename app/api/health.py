from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.session import get_db

router = APIRouter(prefix="/health", tags=["health"])
log = get_logger(__name__)


def get_redis_client():
    from app.infra.redis import get_redis

    return get_redis()


@router.get("/live")
def live() -> dict:
    """Process is up. Does not touch dependencies."""
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db), redis_client=Depends(get_redis_client)):
    """Dependencies reachable? Returns 503 if PostgreSQL or Redis is down."""
    checks: dict[str, str] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001
        log.exception("readiness_database_failed")
        checks["database"] = "error"
    try:
        redis_client.ping()
        checks["redis"] = "ok"
    except Exception:  # noqa: BLE001
        log.exception("readiness_redis_failed")
        checks["redis"] = "error"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if healthy else 503, content={"status": "ok" if healthy else "degraded", "checks": checks})
