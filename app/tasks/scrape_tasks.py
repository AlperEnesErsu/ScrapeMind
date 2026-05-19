"""Scraping tasks — invoked manually from the dashboard or by Beat."""

from __future__ import annotations

import structlog

from app.core.models.user import User
from app.tasks import celery_app

logger = structlog.get_logger()


@celery_app.task(name="scrape.run_for_user", bind=True, max_retries=2)
def run_for_user(self, user_id: int, *, max_results: int = 25) -> dict:
    """Run arXiv scrape for a single user. Wrapped in app context by Celery."""
    from app.modules.scrape.service import scrape_arxiv_for_user

    user = User.query.filter_by(id=user_id, deleted_at=None).first()
    if user is None:
        logger.warning("scrape_user_missing", user_id=user_id)
        return {"hits": 0, "linked": 0, "reason": "user_missing"}
    try:
        return scrape_arxiv_for_user(user, max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        logger.exception("scrape_failed", user_id=user_id)
        # Retry with exponential backoff — arXiv occasionally rate-limits.
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)


@celery_app.task(name="scrape.run_for_all_users")
def run_for_all_users(*, max_results: int = 25) -> dict:
    """Fan out per-user scrape tasks. Beat calls this once a day."""
    users = User.query.filter_by(deleted_at=None, is_active=True).all()
    queued = 0
    for u in users:
        run_for_user.delay(u.id, max_results=max_results)
        queued += 1
    logger.info("scrape_fanout", queued=queued)
    return {"queued": queued}
