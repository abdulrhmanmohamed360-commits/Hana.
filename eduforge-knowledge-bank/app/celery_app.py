
"""Celery application configuration for EduForge Knowledge Core."""
from __future__ import annotations

from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()


def configure_redis_url(url: str) -> tuple[str, dict]:
    """Configure TLS options for secure Redis connections."""
    if not url.startswith("rediss://"):
        return url, {}

    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["ssl_cert_reqs"] = "required"

    updated_url = urlunparse(
        parsed._replace(query=urlencode(params))
    )
    return updated_url, {"ssl_cert_reqs": "required"}


redis_url, redis_ssl = configure_redis_url(settings.redis_url)

celery_app = Celery(
    "eduforge_knowledge_core",
    broker=redis_url,
    backend=redis_url,
    include=["app.tasks"],
)

celery_app.conf.update(
    broker_use_ssl=redis_ssl or None,
    redis_backend_use_ssl=redis_ssl or None,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    task_default_queue="default",
    task_routes={
        "app.tasks.process_ingestion": {"queue": "ingestion"},
        "app.tasks.scan_knowledge_gaps": {"queue": "research"},
        "app.tasks.process_pending_research": {"queue": "research"},
    },
    beat_schedule={
        "scan-knowledge-gaps-periodically": {
            "task": "app.tasks.scan_knowledge_gaps",
            "schedule": crontab(minute="*/30"),
        },
        "process-pending-research-periodically": {
            "task": "app.tasks.process_pending_research",
            "schedule": crontab(minute="*/10"),
        },
        "process-pending-preparations-periodically": {
            "task": "app.tasks.process_pending_preparations",
            "schedule": crontab(minute="*/5"),
        },
        "process-learning-schedules-periodically": {
            "task": "app.tasks.process_learning_schedules",
            "schedule": crontab(minute="*/15"),
        },
        "embed-pending-chunks-periodically": {
            "task": "app.tasks.embed_pending_chunks",
            "schedule": crontab(minute="*/10"),
        },
    },
)
