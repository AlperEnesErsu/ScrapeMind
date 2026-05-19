"""Admin panel for the Celery task system.

Read-only window into the worker pool: who's alive, what's running, what's
scheduled, and what tasks Celery knows about. Uses Celery's inspect API
which talks to workers over the broker — so when no worker is running,
panels look empty rather than error out.
"""

import structlog
from flask import Blueprint, render_template
from flask_login import login_required

from app.core.auth.decorators import permission_required
from app.tasks import celery_app

logger = structlog.get_logger()

tasks_admin_bp = Blueprint("tasks_admin", __name__)


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

    return render_template(
        "tasks_admin/overview.html",
        workers=workers,
        active=active,
        scheduled=scheduled,
        reserved=reserved,
        registered=registered,
        stats=stats,
        beat_schedule=celery_app.conf.beat_schedule,
    )
