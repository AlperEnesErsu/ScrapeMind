"""Author-following tasks (Faz 5.4).

Same six-step shape as `patent_tasks.ingest_for_user` — user guard →
`acquire_user_lock` → `record_scan_run` → `apply_scan_result` → `self.retry` →
`release_user_lock` — with its own lock kind (`"authors"`).

Queue is `scrape` rather than `io`: this hits the same OpenAlex budget as the
academic scan, and putting both on one queue means the deployment-wide rate
limiter throttles a single pool instead of two pools racing each other for the
same tokens.

Beat runs `ingest_for_all_users` at 03:25 — after the academic scan (03:15)
so the two do not contend for that budget at the same moment. Times are
`BABEL_DEFAULT_TIMEZONE` (Europe/Istanbul), not UTC; see
`app/tasks/schedule.py`.
"""

from __future__ import annotations

import structlog

from app.core.models.user import User
from app.tasks import celery_app
from app.tasks.fanout import fan_out

logger = structlog.get_logger()


@celery_app.task(name="authors.ingest_for_user", bind=True, max_retries=2)
def ingest_for_user(self, user_id: int, *, trigger: str = "auto", lock_held: bool = False) -> dict:
    """Fetch new works for every author this user follows.

    `lock_held`/`trigger` follow the same contract as
    `scrape_tasks.run_for_user`.
    """
    from app.modules.scrape.service import (
        acquire_user_lock,
        apply_scan_result,
        ingest_user_authors,
        record_scan_run,
        release_user_lock,
    )

    user = User.query.filter_by(id=user_id, deleted_at=None).first()
    if user is None:
        logger.warning("author_ingest_user_missing", user_id=user_id)
        release_user_lock(user_id, "authors")
        return {"reason": "user_missing"}

    if not lock_held and not acquire_user_lock(user_id, "authors"):
        logger.info("author_ingest_skip_locked", user_id=user_id)
        return {"reason": "already_running"}

    try:
        with record_scan_run(user_id, "authors", trigger=trigger) as run:
            result = ingest_user_authors(user)
            apply_scan_result(run, result)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("author_ingest_failed", user_id=user_id)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)
    finally:
        release_user_lock(user_id, "authors")


@celery_app.task(name="authors.ingest_for_all_users")
def ingest_for_all_users() -> dict:
    """Fan out per-user author ingestion. Beat calls this once a night.

    Unlike the patent fan-out there is no deployment-level short circuit:
    following authors needs no keys and no admin opt-in, so whether there is
    work to do is a per-user question that `ingest_user_authors` answers
    cheaply (one indexed query) when nobody follows anyone.
    """
    queued = fan_out(ingest_for_user)
    return {"queued": queued}
