"""Digest tasks — daily/weekly LLM briefing over each user's newly-surfaced
papers. Fan-out pattern mirrors `scrape_tasks.py`: Beat calls
`run_for_all_users` once a day/week, which queues one `run_for_user` task
per active user.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from app.core.models.user import User
from app.tasks import celery_app
from app.tasks.fanout import fan_out

logger = structlog.get_logger()

_WINDOW = {
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
}


def _window_bounds(period: str) -> tuple[datetime, datetime]:
    """[start, end) for the given period, ending now."""
    end = datetime.now(UTC)
    start = end - _WINDOW.get(period, _WINDOW["daily"])
    return start, end


def _opted_in_user_ids(period: str):
    """Stream ids of active users whose email-digest preference == `period`.

    The digest is opt-in (default "off"), so unlike the scan fan-out we do NOT
    reach every active user — only those who chose this exact cadence in their
    preferences (stored in user_settings.settings["digest"]).
    """
    from app.core.models.settings import UserSettings
    from app.extensions import db

    q = (
        db.session.query(User.id)
        .join(UserSettings, UserSettings.user_id == User.id)
        .filter(
            User.deleted_at.is_(None),
            User.is_active.is_(True),
            UserSettings.settings["digest"].as_string() == period,
        )
        .order_by(User.id)
    )
    for (uid,) in q.yield_per(1000):
        yield uid


@celery_app.task(name="digest.run_for_user", bind=True, max_retries=2)
def run_for_user(self, user_id: int, period: str = "daily") -> dict:
    """Build one user's digest for `period` ("daily" | "weekly").

    Skips (no LLM call, no row written) when the user has no newly-surfaced
    papers in the window, or when `generate_digest` itself declines (AI
    disabled / LLM call failed) — mirrors the "never call for nothing, never
    persist a partial" contract in ai_service.
    """
    from app.modules.scrape.ai_service import generate_digest
    from app.modules.scrape.service import list_user_papers_in_window

    user = User.query.filter_by(id=user_id, deleted_at=None).first()
    if user is None:
        logger.warning("digest_user_missing", user_id=user_id)
        return {"reason": "user_missing"}

    start, end = _window_bounds(period)
    try:
        links = list_user_papers_in_window(user, start, end)
        if not links:
            logger.info("digest_skip_no_new_items", user_id=user_id, period=period)
            return {"reason": "no_new_items"}

        digest = generate_digest(user, links, period=period, period_start=start, period_end=end)
        if digest is None:
            logger.info("digest_skip_generation_failed", user_id=user_id, period=period)
            return {"reason": "generation_failed"}

        from app.core.models.notification import add_notification

        title = "Günlük Brifing Hazır" if period == "daily" else "Haftalık Brifing Hazır"
        preview = (digest.summary or "")[:140]
        add_notification(
            user.id,
            title=title,
            message=f"{preview} /dashboard",
        )

        # Dispatch email digest if recipient email is available
        from app.core.email.service import send_email

        if user.email:
            subject = f"ScrapeMind — {title}"
            body = (
                f"Merhaba {user.full_name or user.username},\n\n"
                f"{digest.summary}\n\n"
                f"Detaylı özetinizi ve yeni makalelerinizi incelemek için ScrapeMind'a giriş yapın:\n"
                f"http://localhost:5000/dashboard\n\n"
                f"İyi çalışmalar,\nScrapeMind Ekibi"
            )
            try:
                send_email(user.email, subject, body)
            except Exception:  # noqa: BLE001
                logger.exception("digest_email_send_failed", user_id=user.id)

        logger.info("digest_done", user_id=user_id, period=period, item_count=digest.item_count)
        return {"digest_id": digest.id, "item_count": digest.item_count}
    except Exception as exc:  # noqa: BLE001
        logger.exception("digest_failed", user_id=user_id, period=period)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)


@celery_app.task(name="digest.run_for_all_users")
def run_for_all_users(period: str = "daily") -> dict:
    """Fan out per-user digest tasks. Beat calls this once a day (period=
    "daily") and once a week (period="weekly").

    Spread over SCAN_FANOUT_WINDOW_SECONDS like the other fan-outs — one LLM
    call per user, so queueing them all in the same instant is exactly what a
    shared API key cannot absorb (see app/tasks/fanout.py).

    Unlike the scan fan-out this is opt-in: only users whose digest preference
    matches `period` receive a task (see `_opted_in_user_ids`).
    """
    queued = fan_out(
        run_for_user,
        args_for=lambda uid: (uid, period),
        user_ids=_opted_in_user_ids(period),
    )
    logger.info("digest_fanout", queued=queued, period=period)
    return {"queued": queued}
