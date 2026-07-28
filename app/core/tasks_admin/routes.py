"""Admin panel for the Celery task system.

Read-only window into the worker pool: who's alive, what's running, what's
scheduled, and what tasks Celery knows about. Uses Celery's inspect API
which talks to workers over the broker — so when no worker is running,
panels look empty rather than error out.
"""

import structlog
from flask import Blueprint, flash, redirect, render_template, url_for
from flask_babel import gettext as _
from flask_login import login_required

from app.core.audit.middleware import log_action
from app.core.auth.decorators import permission_required
from app.tasks import celery_app

logger = structlog.get_logger()

tasks_admin_bp = Blueprint("tasks_admin", __name__)

# Manual triggers exposed on the admin panel. Each maps a form action to the
# Celery task it enqueues + the flash shown on success. Gated by tasks.view —
# the same admin-only surface that already shows the panel.
_TRIGGERS = {
    "scrape_all": ("scrape.run_for_all_users", "task.scrape_all_queued"),
    "purge_audit": ("core.purge_audit_logs", "task.purge_audit_queued"),
}


def _safe_inspect(method: str) -> dict:
    """Call Celery inspect.<method>() defensively.

    Returns {} if no workers are online (typical in dev when worker isn't
    started) or if the broker is unreachable. Never raises.
    """
    inspect = celery_app.control.inspect(timeout=1)
    try:
        result = getattr(inspect, method)()
    except Exception:  # noqa: BLE001 — broker down, eager mode, etc.
        logger.exception("celery_inspect_failed", method=method)
        return {}
    return result or {}


@tasks_admin_bp.route("/")
@login_required
@permission_required("tasks.view")
def overview():
    active = _safe_inspect("active")
    scheduled = _safe_inspect("scheduled")
    reserved = _safe_inspect("reserved")
    registered = _safe_inspect("registered")
    stats = _safe_inspect("stats")

    workers = sorted(set(active) | set(scheduled) | set(reserved) | set(registered) | set(stats))

    # Resolved next-run times + queue per entry, so the schedule can be read
    # without mentally evaluating crontabs in the app timezone.
    try:
        from app.tasks.schedule_info import schedule_overview

        schedule_rows = schedule_overview()
    except Exception:  # noqa: BLE001 — never let this break the admin page
        logger.exception("schedule_overview_failed")
        schedule_rows = []

    return render_template(
        "tasks_admin/overview.html",
        workers=workers,
        active=active,
        scheduled=scheduled,
        reserved=reserved,
        registered=registered,
        stats=stats,
        beat_schedule=celery_app.conf.beat_schedule,
        schedule_rows=schedule_rows,
    )


@tasks_admin_bp.route("/run/<trigger>", methods=["POST"])
@login_required
@permission_required("tasks.view")
def run_task(trigger: str):
    """Manually enqueue one of the whitelisted maintenance tasks.

    The trigger name is looked up in a fixed table — arbitrary task names can
    never be dispatched from the browser.
    """
    entry = _TRIGGERS.get(trigger)
    if entry is None:
        flash(_("Unknown task."), "danger")
        return redirect(url_for("tasks_admin.overview"))

    task_name, flash_key = entry
    async_result = celery_app.send_task(task_name)
    task_id = getattr(async_result, "id", None)
    log_action(
        "task.manual_trigger",
        entity_type="celery_task",
        entity_id=task_name,
        changes={"task_id": task_id},
    )
    logger.info("task_manual_trigger", task=task_name, task_id=task_id)

    messages = {
        "task.scrape_all_queued": _("Scrape queued for all users."),
        "task.purge_audit_queued": _("Audit log cleanup queued."),
    }
    flash(messages[flash_key], "success")
    return redirect(url_for("tasks_admin.overview"))
