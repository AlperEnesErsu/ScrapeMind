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

    Also stamps a Redis key (`app/core/health.py`). That stamp is what the
    sidebar status panel reads: because this task only runs when Beat schedules
    it *and* a worker consumes it, a fresh stamp proves both halves — for one
    O(1) lookup, instead of a broadcast `celery inspect ping` that blocks for
    its whole timeout exactly when nothing is listening.
    """
    from app.core.health import record_heartbeat

    now = datetime.now(UTC).isoformat()
    record_heartbeat(self.request.hostname)
    logger.info("task_heartbeat", worker=self.request.hostname, at=now)
    return {"worker": self.request.hostname, "at": now}


@celery_app.task(name="core.purge_audit_logs")
def purge_audit_logs() -> dict:
    """Nightly retention sweep — drop audit rows older than
    AUDIT_RETENTION_DAYS (0 disables). See app/core/audit/retention.py."""
    from app.core.audit.retention import purge_expired

    deleted = purge_expired()
    return {"deleted": deleted}


@celery_app.task(name="core.send_password_reset")
def send_password_reset_email(user_id: int, reset_url: str) -> bool:
    """Deliver a reset link outside the request that asked for it.

    `/auth/forgot` answers the same text for known and unknown addresses, but
    it used to talk to SMTP only for known ones -- so the response time said
    what the text did not. Queueing makes both branches cost about the same.
    """
    from app.core.email.service import send_password_reset
    from app.core.models.user import User
    from app.extensions import db

    user = db.session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        return False
    sent, _dev_url = send_password_reset(user, reset_url)
    return sent


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
