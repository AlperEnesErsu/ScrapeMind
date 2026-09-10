"""Celery tasks for saved-search alerts (Faz 7.1).

Fans out per user like the digest does, and for the same reason: one task per
user rather than one per saved search, so a user with eight searches gets one
slot in the fan-out window instead of eight.

No LLM call anywhere here -- this is a database query and a notification -- so
the queue is `io` rather than `llm`.
"""

from __future__ import annotations

import structlog

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.alerts import run_saved_search
from app.modules.scrape.models import SavedSearch
from app.tasks import celery_app
from app.tasks.fanout import fan_out

logger = structlog.get_logger()


def _user_ids_with_searches(cadence: str) -> list[int]:
    """Users who have at least one active saved search on this cadence.

    Distinct user ids rather than searches: the fan-out window exists to spread
    load across users, and a user's own searches run together anyway.
    """
    rows = (
        db.session.query(SavedSearch.user_id)
        .filter(
            SavedSearch.is_active.is_(True),
            SavedSearch.cadence == cadence,
            SavedSearch.deleted_at.is_(None),
        )
        .distinct()
        .all()
    )
    return [user_id for (user_id,) in rows]


@celery_app.task(name="alerts.run_for_user")
def run_for_user(user_id: int, cadence: str = "weekly") -> dict:
    """Run every active saved search this user has on `cadence`."""
    user = User.query.filter_by(id=user_id, deleted_at=None).first()
    if user is None:
        logger.warning("alerts_user_missing", user_id=user_id)
        return {"reason": "user_missing"}

    searches = SavedSearch.query.filter_by(
        user_id=user_id, cadence=cadence, is_active=True, deleted_at=None
    ).all()

    notified = 0
    for search in searches:
        try:
            if run_saved_search(search) is not None:
                notified += 1
        except Exception:  # noqa: BLE001 — one bad search must not lose the others
            logger.exception("alerts_search_failed", saved_search_id=search.id)
            db.session.rollback()

    logger.info("alerts_run_for_user", user_id=user_id, searches=len(searches), notified=notified)
    return {"searches": len(searches), "notified": notified}


@celery_app.task(name="alerts.run_for_all_users")
def run_for_all_users(cadence: str = "weekly") -> dict:
    """Fan out per-user alert tasks. Beat calls this daily and weekly.

    Opt-in like the digest: only users with an active saved search on this
    cadence get a task, so the default population is nobody.
    """
    queued = fan_out(
        run_for_user,
        args_for=lambda uid: (uid, cadence),
        user_ids=_user_ids_with_searches(cadence),
    )
    logger.info("alerts_fanout", queued=queued, cadence=cadence)
    return {"queued": queued}
