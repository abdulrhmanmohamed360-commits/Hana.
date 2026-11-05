"""Celery application configuration for EduForge Knowledge Core."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings


settings = get_settings()

celery_app = Celery(
    "eduforge_knowledge_core",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],
)

celery_app.conf.update(
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
    },
)

