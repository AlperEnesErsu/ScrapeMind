"""Scraping tasks — invoked manually from the dashboard or by Beat."""

from __future__ import annotations

import structlog
from flask import current_app

from app.core.models.user import User
from app.tasks import celery_app
from app.tasks.fanout import fan_out

logger = structlog.get_logger()


@celery_app.task(
    name="scrape.run_for_user",
    bind=True,
    max_retries=2,
    soft_time_limit=300,
    time_limit=420,
)
def run_for_user(
    self, user_id: int, *, max_results: int = 25, trigger: str = "auto", lock_held: bool = False
) -> dict:
    """Run all enabled sources for a single user. Wrapped in app context.

    `lock_held` says whether the caller already claimed this user's scrape slot.
    The manual route does (so it can tell the user "already running" straight
    away); the nightly fan-out does not, so the task claims it here. Without
    this the nightly path used to *release* a lock it never took, which quietly
    freed a manual run's slot.

    `trigger` ("auto" | "manual") is recorded on the ScanRun row so the
    dashboard can distinguish a scheduled scan from a button press.
    """
    from app.modules.scrape.service import (
        acquire_user_lock,
        apply_scan_result,
        record_scan_run,
        release_user_lock,
        scrape_for_user,
    )

    user = User.query.filter_by(id=user_id, deleted_at=None).first()
    if user is None:
        logger.warning("scrape_user_missing", user_id=user_id)
        release_user_lock(user_id, "scrape")
        return {"hits": 0, "linked": 0, "reason": "user_missing"}

    if not lock_held and not acquire_user_lock(user_id, "scrape"):
        logger.info("scrape_skip_locked", user_id=user_id)
        return {"hits": 0, "linked": 0, "reason": "already_running"}

    try:
        with record_scan_run(user_id, "scrape", trigger=trigger) as run:
            res = scrape_for_user(user, max_results=max_results)
            apply_scan_result(run, res)

        from app.core.models.notification import add_notification

        # Kaç source başarıyla çalıştı? (-1 = o source hata verdi)
        sources = res.get("sources", {})
        ok_sources = [name for name, n in sources.items() if n >= 0]
        add_notification(
            user.id,
            title="Tarama Tamamlandı",
            message=(
                f"Tarama tamamlandı ({', '.join(ok_sources) or 'kaynak yok'}). "
                f"{res.get('linked', 0)} yeni makale feed'inize eklendi."
            ),
        )
        return res
    except Exception as exc:  # noqa: BLE001
        logger.exception("scrape_failed", user_id=user_id)
        # Retry with exponential backoff — arXiv occasionally rate-limits.
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)
    finally:
        # Free the slot in every exit path, including before a retry: the retry
        # runs later and we don't want manual scrapes blocked across the backoff.
        release_user_lock(user_id, "scrape")


@celery_app.task(name="scrape.run_for_all_users")
def run_for_all_users(*, max_results: int = 25) -> dict:
    """Fan out per-user scrape tasks. Beat calls this once a day.

    Users are spread deterministically over SCAN_FANOUT_WINDOW_SECONDS rather
    than all queued at once — see app/tasks/fanout.py for why the offset is
    derived from the user id instead of being random.
    """
    queued = fan_out(run_for_user, kwargs_for=lambda uid: {"max_results": max_results})
    return {"queued": queued}


@celery_app.task(name="scrape.purge_scan_runs")
def purge_scan_runs() -> dict:
    """Drop scan-history rows past SCAN_RUN_RETENTION_DAYS. 0 = keep forever.
    Mirrors `core.purge_audit_logs`."""
    from app.modules.scrape.service import purge_scan_runs as _purge
    from app.modules.scrape.service import reap_stale_runs

    # Close out anything a killed worker left open, deployment-wide. The status
    # page reaps lazily for the user reading it; this catches everyone else.
    reaped = reap_stale_runs()
    days = int(current_app.config.get("SCAN_RUN_RETENTION_DAYS", 30))
    deleted = _purge(days)
    logger.info("scan_runs_purged", deleted=deleted, reaped=reaped, retention_days=days)
    return {"deleted": deleted, "reaped": reaped, "retention_days": days}
