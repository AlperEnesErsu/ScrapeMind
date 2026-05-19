"""Papers UI — the user-facing read view + a manual "scrape now" trigger."""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, flash, redirect, render_template, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.extensions import db
from app.modules.scrape.models import UserPaper
from app.modules.scrape.service import list_user_papers

scrape_bp = Blueprint("scrape", __name__)


@scrape_bp.route("/")
@login_required
def feed():
    rows = list_user_papers(current_user, limit=100)
    return render_template("scrape/feed.html", rows=rows)


@scrape_bp.route("/<int:user_paper_id>/open", methods=["POST"])
@login_required
def open_paper(user_paper_id: int):
    """Mark a row as seen and redirect to the paper's source URL."""
    link = UserPaper.query.filter_by(id=user_paper_id, user_id=current_user.id).first()
    if link is None:
        flash(_("Paper not found."), "danger")
        return redirect(url_for("scrape.feed"))
    if link.seen_at is None:
        link.seen_at = datetime.now(UTC)
        db.session.commit()
    target = link.paper.url or url_for("scrape.feed")
    return redirect(target)


@scrape_bp.route("/run", methods=["POST"])
@login_required
def run_now():
    """Queue a one-off scrape for the current user."""
    from app.tasks.scrape_tasks import run_for_user

    async_result = run_for_user.delay(current_user.id)
    log_action(
        "scrape.manual_run",
        entity_type="user",
        entity_id=str(current_user.id),
        changes={"task_id": getattr(async_result, "id", None)},
    )
    flash(_("Scrape queued — papers will appear here once the worker finishes."), "info")
    return redirect(url_for("scrape.feed"))
