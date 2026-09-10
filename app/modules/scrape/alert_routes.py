"""Routes for saved searches (Faz 7.1).

Creation happens from the library search page, so a saved search is always the
search the user is already looking at rather than a form they fill in twice.
Management lives in a profile tab, registered the same way the AI and followed-
author tabs are.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.extensions import db
from app.modules.scrape.alerts import (
    ALLOWED_FILTERS,
    VALID_CADENCES,
    AlertResult,
    find_new_matches,
)
from app.modules.scrape.models import SavedSearch

alerts_bp = Blueprint("alerts", __name__)

#: How many saved searches one user may keep.
#:
#: Each one is a query per cadence run, so this is a load ceiling as much as a
#: tidiness one. Generous enough that nobody legitimate meets it.
MAX_SAVED_SEARCHES = 20


def _filters_from_request() -> dict:
    """Pull the library search's filters out of the submitted form.

    Only `ALLOWED_FILTERS` survives, and the whitelist lives in `alerts.py`
    rather than here so the query builder and the form cannot disagree about
    what a saved search may carry.
    """
    raw = {
        "source": (request.form.get("source") or "").strip(),
        "quartile": (request.form.get("quartile") or "").strip().upper(),
        "date_from": (request.form.get("from") or "").strip(),
        "date_to": (request.form.get("to") or "").strip(),
        "has_notes": request.form.get("has_notes") == "1",
    }
    if raw["quartile"] not in ("Q1", "Q2", "Q3", "Q4"):
        raw["quartile"] = ""
    return {k: v for k, v in raw.items() if k in ALLOWED_FILTERS and v not in ("", False, None)}


@alerts_bp.route("/saved-searches", methods=["POST"])
@login_required
def create():
    """Save the search currently on screen."""
    name = (request.form.get("name") or "").strip()
    q = (request.form.get("q") or "").strip()
    cadence = (request.form.get("cadence") or "weekly").strip()
    semantic = request.form.get("semantic") == "1"

    if cadence not in VALID_CADENCES:
        cadence = "weekly"

    if not name:
        flash(_("Give the saved search a name."), "warning")
        return redirect(request.referrer or url_for("library.search"))

    existing = SavedSearch.query.filter_by(user_id=current_user.id, deleted_at=None).count()
    if existing >= MAX_SAVED_SEARCHES:
        flash(
            _("You already have %(count)d saved searches, which is the maximum.", count=existing),
            "warning",
        )
        return redirect(request.referrer or url_for("library.search"))

    clash = SavedSearch.query.filter_by(user_id=current_user.id, name=name, deleted_at=None).first()
    if clash is not None:
        flash(_("You already have a saved search called “%(name)s”.", name=name), "warning")
        return redirect(request.referrer or url_for("library.search"))

    search = SavedSearch(
        user_id=current_user.id,
        name=name,
        q=q or None,
        filters=_filters_from_request() or None,
        semantic=semantic,
        cadence=cadence,
    )
    db.session.add(search)
    db.session.commit()

    log_action("saved_search.create", entity_type="saved_search", entity_id=str(search.id))
    flash(_("Saved. You will be told when new papers match it."), "success")
    return redirect(request.referrer or url_for("library.search"))


def _own_search_or_404(search_id: int) -> SavedSearch:
    search = SavedSearch.query.filter_by(
        id=search_id, user_id=current_user.id, deleted_at=None
    ).first()
    if search is None:
        abort(404)
    return search


@alerts_bp.route("/saved-searches/<int:search_id>/cadence", methods=["POST"])
@login_required
def set_cadence(search_id: int):
    search = _own_search_or_404(search_id)
    cadence = (request.form.get("cadence") or "").strip()
    if cadence not in VALID_CADENCES:
        abort(400)

    search.cadence = cadence
    # "off" is a pause, not a delete: the announced-set is kept, so switching
    # back on does not re-announce everything the user has already seen.
    db.session.commit()

    log_action("saved_search.set_cadence", entity_type="saved_search", entity_id=str(search.id))
    flash(_("Updated."), "success")
    return redirect(url_for("settings.profile", tab="alerts"))


@alerts_bp.route("/saved-searches/<int:search_id>/delete", methods=["POST"])
@login_required
def delete(search_id: int):
    search = _own_search_or_404(search_id)
    name = search.name
    db.session.delete(search)
    db.session.commit()

    log_action("saved_search.delete", entity_type="saved_search", entity_id=str(search_id))
    flash(_("Deleted “%(name)s”.", name=name), "success")
    return redirect(url_for("settings.profile", tab="alerts"))


def alerts_tab_ctx() -> dict:
    """Context for the profile tab.

    `pending` runs each search without announcing, so the tab can show what an
    alert would say right now -- which is the only way a user can tell whether
    a query is too wide before the first notification arrives.
    """
    searches = (
        SavedSearch.query.filter_by(user_id=current_user.id, deleted_at=None)
        .order_by(SavedSearch.created_at.desc())
        .all()
    )
    pending: dict[int, AlertResult | None] = {}
    for search in searches:
        try:
            pending[search.id] = find_new_matches(search)
        except Exception:  # noqa: BLE001 — a broken saved search must still be deletable
            pending[search.id] = None
    return {
        "saved_searches": searches,
        "pending": pending,
        "cadences": VALID_CADENCES,
        "max_saved_searches": MAX_SAVED_SEARCHES,
    }
