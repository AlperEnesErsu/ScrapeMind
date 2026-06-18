"""Papers UI — the user-facing read view + per-paper actions.

Route layout:
    /papers/                      → Discover feed (default view)
    /papers/?view=favorites       → starred only
    /papers/?view=dismissed       → hidden bin (recovery)
    /papers/<id>                  → paper detail (read + notes panel)
    /papers/<id>/open             → mark seen and redirect to source URL
    /papers/<id>/favorite/toggle  → HTMX: flip star, swap card actions
    /papers/<id>/dismiss          → HTMX: hide from feed, swap to undo banner
    /papers/<id>/undismiss        → HTMX: put back
    /papers/<id>/notes            → HTMX: POST adds, GET lists (partial)
    /papers/notes/<note_id>       → HTMX: DELETE drops a note
    /papers/run                   → POST queue a one-off scrape
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape.service import (
    add_note,
    delete_note,
    get_note_for_user,
    get_user_paper,
    list_user_papers,
    mark_seen,
    set_dismissed,
    toggle_favorite,
)

scrape_bp = Blueprint("scrape", __name__, template_folder="templates")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _render_card(link, *, flash_msg=None, flash_kind=None):
    """Render a single paper card — swap target for HTMX action handlers."""
    return render_template(
        "scrape/_paper_card.html",
        r=link,
        flash_msg=flash_msg,
        flash_kind=flash_kind,
    )


# ----------------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------------


@scrape_bp.route("/")
@login_required
def feed():
    view = request.args.get("view", "discover")
    if view not in {"discover", "favorites", "dismissed", "all"}:
        view = "discover"
    rows = list_user_papers(current_user, limit=100, view=view)
    counts = {
        "discover": len(list_user_papers(current_user, limit=500, view="discover")),
        "favorites": len(list_user_papers(current_user, limit=500, view="favorites")),
    }
    return render_template("scrape/feed.html", rows=rows, view=view, counts=counts)


@scrape_bp.route("/<int:user_paper_id>")
@login_required
def detail(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    mark_seen(link)
    return render_template("scrape/detail.html", r=link)


# ----------------------------------------------------------------------------
# Per-paper actions — HTMX swaps the card
# ----------------------------------------------------------------------------


@scrape_bp.route("/<int:user_paper_id>/open", methods=["POST"])
@login_required
def open_paper(user_paper_id: int):
    """Mark seen and redirect to source URL (non-HTMX, full nav)."""
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        flash(_("Paper not found."), "danger")
        return redirect(url_for("scrape.feed"))
    mark_seen(link)
    target = link.paper.url or url_for("scrape.feed")
    return redirect(target)


@scrape_bp.route("/<int:user_paper_id>/favorite/toggle", methods=["POST"])
@login_required
def toggle_favorite_route(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    is_now_fav = toggle_favorite(link)
    log_action(
        "paper.favorite_toggled",
        entity_type="user_paper",
        entity_id=str(link.id),
        changes={"is_favorite": is_now_fav},
    )
    if _is_htmx():
        return _render_card(link)
    flash(_("Saved to favorites.") if is_now_fav else _("Removed from favorites."), "success")
    return redirect(request.referrer or url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/dismiss", methods=["POST"])
@login_required
def dismiss(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    set_dismissed(link, True)
    log_action("paper.dismissed", entity_type="user_paper", entity_id=str(link.id))
    if _is_htmx():
        # Render an undo banner that replaces the card in-place; clicking
        # it un-dismisses without losing the slot.
        return render_template("scrape/_dismissed_undo.html", r=link)
    flash(_("Hidden from your feed."), "info")
    return redirect(url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/undismiss", methods=["POST"])
@login_required
def undismiss(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    set_dismissed(link, False)
    log_action("paper.undismissed", entity_type="user_paper", entity_id=str(link.id))
    if _is_htmx():
        return _render_card(link)
    return redirect(url_for("scrape.feed"))


# ----------------------------------------------------------------------------
# Notes — HTMX add/delete inside paper detail
# ----------------------------------------------------------------------------


@scrape_bp.route("/<int:user_paper_id>/notes", methods=["POST"])
@login_required
def add_note_route(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    body = request.form.get("body", "")
    tag = request.form.get("tag", "")
    note = add_note(link, body, tag=tag)
    if note is None:
        # Empty body — re-render the list unchanged with a small inline message
        if _is_htmx():
            return render_template(
                "scrape/_notes_list.html",
                r=link,
                flash_msg=_("Empty notes are not saved."),
                flash_kind="danger",
            )
        flash(_("Empty notes are not saved."), "danger")
        return redirect(url_for("scrape.detail", user_paper_id=link.id))
    log_action(
        "paper.note_added",
        entity_type="paper_note",
        entity_id=str(note.id),
        changes={"tag": note.tag},
    )
    if _is_htmx():
        return render_template("scrape/_notes_list.html", r=link)
    return redirect(url_for("scrape.detail", user_paper_id=link.id))


@scrape_bp.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note_route(note_id: int):
    note = get_note_for_user(current_user, note_id)
    if note is None:
        abort(404)
    parent = note.user_paper
    delete_note(note)
    log_action("paper.note_deleted", entity_type="paper_note", entity_id=str(note_id))
    if _is_htmx():
        return render_template("scrape/_notes_list.html", r=parent)
    return redirect(url_for("scrape.detail", user_paper_id=parent.id))


# ----------------------------------------------------------------------------
# Manual scrape
# ----------------------------------------------------------------------------


@scrape_bp.route("/run", methods=["POST"])
@login_required
def run_now():
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
