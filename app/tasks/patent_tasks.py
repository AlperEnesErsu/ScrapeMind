"""Patent scanning tasks (Faz 5.2).

Same six-step shape as `channel_tasks.ingest_for_user` — user guard →
`acquire_user_lock` → `record_scan_run` → `apply_scan_result` → `self.retry` →
`release_user_lock` — with its own lock kind (`"patents"`) so a patent run and
an academic run for the same user never collapse into each other.

Why a separate task rather than folding the patent adapters into
`scrape.run_for_user`: the patent sources are gated (keys + an admin opt-in),
metered against a weekly budget, and off for most deployments. Sharing a
ScanRun with the academic sources would mean one exhausted patent quota marks
the whole academic scan `partial`, and the status line stops meaning anything.
Separate `ScanRun.kind="patents"` rows keep the two answers independent.

Beat runs `ingest_for_all_users` at 03:05 — between channel ingestion (02:55)
and the academic scan (03:15). Those times are `BABEL_DEFAULT_TIMEZONE`
(Europe/Istanbul), not UTC; see `app/tasks/schedule.py`.
"""

from __future__ import annotations

import structlog

from app.core.models.user import User
from app.tasks import celery_app
from app.tasks.fanout import fan_out

logger = structlog.get_logger()


@celery_app.task(name="patents.ingest_for_user", bind=True, max_retries=2)
def ingest_for_user(self, user_id: int, *, trigger: str = "auto", lock_held: bool = False) -> dict:
    """Scan the patent sources for one user's keywords.

    `lock_held`/`trigger` follow the same contract as
    `scrape_tasks.run_for_user`: a caller that already holds this user's lock
    (the manual-trigger route) passes `lock_held=True` so the task does not
    fight itself for it.

    Short-circuits before doing any work when the deployment has no patent
    sources available — no keys, or the admin opt-in is off. That is the
    common case, and it must be cheap and silent rather than a nightly row of
    `skipped` runs for every user.
    """
    from app.modules.scrape.service import (
        acquire_user_lock,
        apply_scan_result,
        record_scan_run,
        release_user_lock,
        scrape_patents_for_user,
    )

    user = User.query.filter_by(id=user_id, deleted_at=None).first()
    if user is None:
        logger.warning("patent_ingest_user_missing", user_id=user_id)
        release_user_lock(user_id, "patents")
        return {"reason": "user_missing"}

    if not lock_held and not acquire_user_lock(user_id, "patents"):
        logger.info("patent_ingest_skip_locked", user_id=user_id)
        return {"reason": "already_running"}

    try:
        with record_scan_run(user_id, "patents", trigger=trigger) as run:
            result = scrape_patents_for_user(user)
            apply_scan_result(run, result)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("patent_ingest_failed", user_id=user_id)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)
    finally:
        release_user_lock(user_id, "patents")


@celery_app.task(name="patents.ingest_for_all_users")
def ingest_for_all_users() -> dict:
    """Fan out per-user patent scanning. Beat calls this once a night.

    Returns without queueing anything when no patent source is available, so a
    deployment that never configured EPO/PatentsView keys pays nothing for
    this schedule entry beyond one registry lookup.
    """
    from app.modules.scrape.service import patent_sources_available

    if not patent_sources_available():
        logger.info("patent_ingest_no_sources")
        return {"queued": 0, "reason": "no_patent_sources"}

    queued = fan_out(ingest_for_user)
    return {"queued": queued}
