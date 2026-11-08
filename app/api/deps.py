from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.errors import RateLimitedError
from app.core.rate_limit import MemoryRateLimiter, RateLimiter, RedisRateLimiter
from app.core.security import Principal, Role, get_principal, require_role  # noqa: F401 (re-exported)
from app.db.session import get_db  # noqa: F401 (re-exported)


@lru_cache
def get_rate_limiter() -> RateLimiter:
    s = get_settings()
    if s.rate_limit_backend == "redis":
        from app.infra.redis import get_redis

        return RedisRateLimiter(get_redis(), s.rate_limit_requests, s.rate_limit_window_seconds)
    return MemoryRateLimiter(s.rate_limit_requests, s.rate_limit_window_seconds)


def enforce_rate_limit(
    request: Request,
    principal: Principal = Depends(get_principal),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    """Runs after authentication: limit is per role-key identity + client IP."""
    client = request.client.host if request.client else "unknown"
    decision = limiter.hit(f"{principal.name}:{client}")
    if not decision.allowed:
        raise RateLimitedError("Rate limit exceeded", headers={"Retry-After": str(max(decision.retry_after, 1))})


def get_llm():
    """Schema-validated LLM runner, or None when LLM_API_KEY is not configured."""
    from app.agents.llm import build_llm
    return build_llm(get_settings())


def get_embedder():
    from app.services.embeddings import build_embedder
    return build_embedder(get_settings())


def get_job_dispatcher():
    """Callable that enqueues a preparation job on Celery. Overridable in tests."""
    def dispatch(job_id: str) -> None:
        from app.tasks import run_preparation_job
        run_preparation_job.delay(job_id)
    return dispatch


def get_scan_dispatcher():
    """Callable that enqueues a continuous-learning scan on Celery. Overridable in tests."""
    def dispatch(scan_id: str) -> None:
        from app.tasks import run_learning_scan
        run_learning_scan.delay(scan_id)
    return dispatch
