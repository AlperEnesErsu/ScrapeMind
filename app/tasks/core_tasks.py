"""Core platform tasks — smoke tests + ops utilities.

Real scraping tasks land in app/tasks/scrape_tasks.py in slice 3.
"""

from datetime import UTC, datetime

import structlog

from app.tasks import celery_app

logger = structlog.get_logger()


@celery_app.task(name="core.ping")
def ping() -> str:
    """Trivial liveness probe."""
    logger.info("task_ping")
    return "pong"


@celery_app.task(name="core.heartbeat", bind=True)
def heartbeat(self) -> dict:
    """Periodic heartbeat — runs every minute via beat schedule.

    Returns the worker hostname and ISO timestamp; useful for the admin
    /admin/tasks/ panel to confirm a worker is alive without a real job.
    """
    now = datetime.now(UTC).isoformat()
    logger.info("task_heartbeat", worker=self.request.hostname, at=now)
    return {"worker": self.request.hostname, "at": now}
