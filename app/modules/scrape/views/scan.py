"""Manual scan trigger and scan-status polling handlers."""

from __future__ import annotations

import time

import structlog
from flask import flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape import routes
from app.modules.scrape.routes import scrape_bp

logger = structlog.get_logger()


@scrape_bp.route("/scan-status", methods=["GET"])
@login_required
def scan_status():
    """The last/next-scan line on its own, so it can poll itself while a scan
    is actually in flight and then swap in the finished result.

    Lives in the scrape blueprint (scrape owns the data) but renders the
    dashboard partial, mirroring how `_sources_card.html` is shared.
    """
    from app.modules.scrape.service import scan_status_context

    return render_template("dashboard/_scan_status.html", **scan_status_context(current_user))


# ----------------------------------------------------------------------------
# Manual scrape
# ----------------------------------------------------------------------------


@scrape_bp.route("/run", methods=["POST"])
@login_required
def run_now():
    from app.modules.scrape.service import acquire_scrape_lock
    from app.tasks.scrape_tasks import run_for_user

    # Collapse duplicate presses: if a scrape is already queued/running for this
    # user, don't stack another task — just tell them it's already in progress.
    if not acquire_scrape_lock(current_user.id):
        msg = _("A scrape is already running for you — you'll be notified when it finishes.")
        if routes._is_htmx():
            return render_template(
                "scrape/_scraper_widget.html",
                already_running=True,
                **routes._widget_ctx(),
            )
        flash(msg, "info")
        return redirect(url_for("scrape.feed"))

    # lock_held: the lock was just claimed above, so the task must not try to
    # claim it again (and must still release it when it finishes).
    async_result = run_for_user.delay(current_user.id, trigger="manual", lock_held=True)

    # RSS is a separate pipeline (global ingest + per-user relevance linking),
    # so pressing "Scrape now" used to refresh the academic sources only and
    # silently leave the news feeds at last night's state — "I just scanned,
    # there's nothing new" was wrong rather than empty. Queue the feed task
    # too. It takes its own "feeds" lock, so an already-running feed pass makes
    # this a no-op instead of a double fetch + double LLM spend. Failure to
    # queue it must not cost the user their scrape, which is already away.
    feed_task_id = None
    try:
        from app.tasks.feed_tasks import link_for_user

        feed_task_id = getattr(link_for_user.delay(current_user.id), "id", None)
    except Exception:  # noqa: BLE001
        logger.exception("manual_feed_refresh_enqueue_failed", user_id=current_user.id)

    # Subscribed YouTube channels are a third pipeline, and they had exactly the
    # bug described above for RSS: "Scrape now" left them at last night's state,
    # so a user who had just subscribed to a channel pressed scrape, saw nothing,
    # and had no way to tell whether it had been checked at all. Same contract as
    # the feed task — its own "channels" lock makes a concurrent run a no-op, and
    # a failure to queue must not cost the user the scrape that is already away.
    channel_task_id = None
    try:
        from app.tasks.channel_tasks import ingest_for_user as ingest_channels_for_user

        channel_task_id = getattr(
            ingest_channels_for_user.delay(current_user.id, trigger="manual"), "id", None
        )
    except Exception:  # noqa: BLE001
        logger.exception("manual_channel_refresh_enqueue_failed", user_id=current_user.id)

    log_action(
        "scrape.manual_run",
        entity_type="user",
        entity_id=str(current_user.id),
        changes={
            "task_id": getattr(async_result, "id", None),
            "feed_task_id": feed_task_id,
            "channel_task_id": channel_task_id,
        },
    )
    if routes._is_htmx():
        return render_template(
            "scrape/_scrape_spinner.html",
            task_id=async_result.id,
            since=int(time.time()),
            phase="queued",
        )
    flash(_("Scrape queued — papers will appear here once the worker finishes."), "info")
    return redirect(url_for("scrape.feed"))


#: How long a manually-triggered scrape may sit in the queue before we stop
#: spinning and say so. Anything longer means nothing is consuming the queue —
#: almost always "the Celery worker isn't running" — and an endless spinner is
#: the least useful way to communicate that.
_QUEUE_WAIT_LIMIT = 90


def _celery_task_finished(task_id: str) -> bool | None:
    """Has this Celery task finished? None when we can't tell.

    `AsyncResult(task_id)` without an explicit app resolves through
    `celery.current_app`, which in a web process can be a bare default Celery
    app whose backend is `DisabledBackend` — calling `.ready()` on that raises
    AttributeError and 500s the poll. Bind our configured app explicitly, and
    treat any backend failure as "unknown" so the DB fallback decides.
    """
    from celery.result import AsyncResult

    from app.tasks import celery_app

    try:
        return bool(AsyncResult(task_id, app=celery_app).ready())
    except Exception:  # noqa: BLE001 — no/unreachable result backend
        logger.warning("scrape_status_backend_unavailable", task_id=task_id)
        return None


@scrape_bp.route("/status/<task_id>", methods=["GET"])
@login_required
def scrape_status_poll(task_id: str):
    """Drive the manual-scrape spinner.

    Celery's result backend is treated as a hint, not the source of truth: the
    ScanRun rows say what actually happened, and they exist even when no result
    backend is configured. That also lets us distinguish "queued" from
    "scanning", and give up honestly when nothing ever picks the task up.
    """
    from flask import Response

    from app.modules.scrape.service import (
        aware,
        last_scan_run,
        open_scan_run,
        reap_stale_runs,
    )

    since = request.args.get("since", type=int) or int(time.time())
    waited = max(0, int(time.time()) - since)

    reap_stale_runs(current_user)
    active = open_scan_run(current_user, "scrape")
    finished = last_scan_run(current_user, "scrape")

    # 5s of slack: the row is written a moment after we stamped `since`, and
    # clock resolution between the two shouldn't decide whether we say "done".
    started_since_request = (
        finished is not None and aware(finished.started_at).timestamp() >= since - 5
    )
    done = routes._celery_task_finished(task_id) is True or started_since_request

    if done:
        response = Response(render_template("scrape/_scraper_widget.html", **routes._widget_ctx()))
        response.headers["HX-Refresh"] = "true"
        return response

    if active is None and waited > routes._QUEUE_WAIT_LIMIT:
        # Nothing claimed the task within the window: stop spinning and say so
        # rather than implying work is happening.
        return render_template(
            "scrape/_scraper_widget.html",
            worker_stalled=True,
            **routes._widget_ctx(),
        )

    return render_template(
        "scrape/_scrape_spinner.html",
        task_id=task_id,
        since=since,
        phase="running" if active is not None else "queued",
    )
