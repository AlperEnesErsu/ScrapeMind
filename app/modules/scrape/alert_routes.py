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

#: Views the Zotero export accepts. Imported from library_routes rather than
#: restated, so the file export and the Zotero export can never offer
#: different shelves under the same name.
from app.modules.scrape.library_routes import _EXPORT_VIEWS  # noqa: E402

#: Ceiling on one export. Zotero writes in batches of 50, and an unbounded
#: export of a large library would be a long synchronous request against
#: somebody else's API.
_ZOTERO_EXPORT_LIMIT = 200


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


# --------------------------------------------------------------------------
# Zotero (Faz 7.2)
#
# These live here rather than in a blueprint of their own because this one is
# already mounted at /library and already carries the library's credentialed
# actions. Two routes do not earn a third blueprint.
# --------------------------------------------------------------------------


@alerts_bp.route("/zotero/credentials", methods=["POST"])
@login_required
def zotero_credentials():
    """Store or clear the user's Zotero API key."""
    from app.modules.scrape.zotero import set_credentials

    api_key = request.form.get("zotero_api_key") or ""
    zotero_user_id = request.form.get("zotero_user_id") or ""

    if api_key.strip() and not zotero_user_id.strip():
        flash(_("Zotero needs both the API key and your numeric user ID."), "warning")
        return redirect(url_for("settings.profile", tab="zotero"))

    set_credentials(current_user, api_key, zotero_user_id)

    # Deliberately not logging which key, only that one was set -- the audit
    # trail should record the act, never the secret.
    log_action(
        "zotero.credentials_set" if api_key.strip() else "zotero.credentials_cleared",
        entity_type="user",
        entity_id=str(current_user.id),
    )
    flash(_("Saved.") if api_key.strip() else _("Zotero disconnected."), "success")
    return redirect(url_for("settings.profile", tab="zotero"))


@alerts_bp.route("/zotero/export", methods=["POST"])
@login_required
def zotero_export():
    """Push the current library view to Zotero.

    User-triggered by design: nothing in this application writes to somebody's
    reference manager on a schedule.
    """
    from app.modules.scrape.service import list_user_papers
    from app.modules.scrape.zotero import ZoteroError, export_papers

    view = (request.form.get("view") or "all").strip()
    service_view = _EXPORT_VIEWS.get(view)
    if service_view is None:
        abort(404)

    rows = list_user_papers(current_user, limit=_ZOTERO_EXPORT_LIMIT, view=service_view)
    papers = [r.paper for r in rows if r.paper is not None]

    try:
        result = export_papers(current_user, papers)
    except ZoteroError as exc:
        flash(_("Zotero export failed: %(reason)s", reason=str(exc)), "danger")
        return redirect(request.referrer or url_for("library.index"))

    log_action("zotero.export", entity_type="user", entity_id=str(current_user.id))

    if result.ok:
        flash(
            _(
                "Sent to Zotero: %(created)d new, %(updated)d updated.",
                created=result.created,
                updated=result.updated,
            ),
            "success",
        )
    else:
        # Counts, not a bare failure: "37 of 40" is the difference between a
        # usable feature and a mystery.
        flash(
            _(
                "Sent to Zotero: %(done)d of %(total)d. %(failed)d failed.",
                done=result.created + result.updated,
                total=result.total,
                failed=result.failed,
            ),
            "warning",
        )
    return redirect(request.referrer or url_for("library.index"))


def zotero_tab_ctx() -> dict:
    from app.modules.scrape.zotero import get_credentials

    credentials = get_credentials(current_user)
    return {
        # Never the key itself -- the template shows whether one exists, and
        # the numeric id, which is not a secret.
        "zotero_connected": credentials is not None,
        "zotero_user_id": credentials[1] if credentials else "",
        "export_limit": _ZOTERO_EXPORT_LIMIT,
    }
