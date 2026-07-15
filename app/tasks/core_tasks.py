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


@celery_app.task(name="core.purge_audit_logs")
def purge_audit_logs() -> dict:
    """Nightly retention sweep — drop audit rows older than
    AUDIT_RETENTION_DAYS (0 disables). See app/core/audit/retention.py."""
    from app.core.audit.retention import purge_expired

    deleted = purge_expired()
    return {"deleted": deleted}


@celery_app.task(name="core.purge_revoked_tokens")
def purge_revoked_tokens() -> dict:
    """Drop denylist rows whose token has expired anyway — past `exp` the
    JWT fails validation on its own, so the row stops carrying weight."""
    from datetime import UTC, datetime

    from app.core.models.revoked_token import RevokedToken
    from app.extensions import db

    deleted = RevokedToken.query.filter(RevokedToken.expires_at < datetime.now(UTC)).delete(
        synchronize_session=False
    )
    db.session.commit()
    if deleted:
        logger.info("revoked_tokens_purged", deleted=deleted)
    return {"deleted": deleted}
