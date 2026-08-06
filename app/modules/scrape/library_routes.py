"""Library — the user-facing read view that wraps Discover/Favorites/Notes/
Timeline tabs in one URL.

The discover feed at `/papers/` is the daily-read entry point. Library is
the *retrospective* lens: what have I starred / written / been shown over
the last weeks.

URL layout:
    /library/                  → Timeline (default)
    /library/?view=favorites   → starred papers
    /library/?view=notes       → notes across every paper
    /library/?view=hidden      → recovery bin for dismissed papers
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.extensions import db
from app.modules.scrape import collections_service as cs
from app.modules.scrape.models import Collection
from app.modules.scrape.service import (
    build_timeline,
    count_all_notes,
    count_user_papers,
    distinct_user_sources,
    list_all_notes,
    list_user_papers,
    papers_to_bibtex,
    papers_to_csv,
    search_user_papers_query,
)

library_bp = Blueprint("library", __name__, template_folder="templates")

_VALID_VIEWS = {"timeline", "favorites", "read_later", "notes", "hidden"}

# Which library views can be exported, mapped to the service-layer view name.
# "all" is everything not dismissed — the whole personal library.
_EXPORT_VIEWS = {
    "all": "discover",
    "favorites": "favorites",
    "read_later": "read_later",
}
# Cap the export so a runaway library can't materialise unbounded rows.
_EXPORT_LIMIT = 5000


def _build_heatmap_data(user):
    from datetime import date, datetime, timedelta

    from sqlalchemy import func

    from app.extensions import db
    from app.modules.scrape.models import PaperNote, UserPaper

    one_year_ago = datetime.utcnow() - timedelta(days=365)

    fav_counts = (
        db.session.query(func.date(UserPaper.created_at), func.count(UserPaper.id))
        .filter(
            UserPaper.user_id == user.id,
            UserPaper.is_favorite.is_(True),
            UserPaper.created_at >= one_year_ago,
        )
        .group_by(func.date(UserPaper.created_at))
        .all()
    )

    note_counts = (
        db.session.query(func.date(PaperNote.created_at), func.count(PaperNote.id))
        .join(UserPaper)
        .filter(UserPaper.user_id == user.id, PaperNote.created_at >= one_year_ago)
        .group_by(func.date(PaperNote.created_at))
        .all()
    )

    activity = {}
    for d_str, count in fav_counts:
        d = d_str if isinstance(d_str, date) else datetime.strptime(d_str, "%Y-%m-%d").date()
        activity[d] = activity.get(d, 0) + count

    for d_str, count in note_counts:
        d = d_str if isinstance(d_str, date) else datetime.strptime(d_str, "%Y-%m-%d").date()
        activity[d] = activity.get(d, 0) + count

    today = date.today()
    start_date = today - timedelta(days=364)

    # Align to starting day of week (Monday)
    weekday_offset = start_date.weekday()
    start_date = start_date - timedelta(days=weekday_offset)

    days_data = []
    curr = start_date
    # Generate 53 full weeks * 7 days = 371 days to keep grid completely aligned
    while len(days_data) < 371:
        count = activity.get(curr, 0)
        if count == 0:
            level = 0
        elif count <= 1:
            level = 1
        elif count <= 3:
            level = 2
        elif count <= 5:
            level = 3
        else:
            level = 4
        days_data.append(
            {
                "date": curr.strftime("%Y-%m-%d") if curr <= today else "",
                "count": count if curr <= today else 0,
                "level": level if curr <= today else 0,
            }
        )
        curr += timedelta(days=1)

    return days_data


@library_bp.route("/")
@login_required
def index():
    view = request.args.get("view", "timeline")
    if view not in _VALID_VIEWS:
        view = "timeline"

    # Counts power the tab badges — SQL count, not Python len(materialized).
    counts = {
        "favorites": count_user_papers(current_user, view="favorites"),
        "read_later": count_user_papers(current_user, view="read_later"),
        "notes": count_all_notes(current_user),
        "hidden": count_user_papers(current_user, view="dismissed"),
    }

    heatmap_days = _build_heatmap_data(current_user)

    ctx: dict = {"view": view, "counts": counts, "heatmap_days": heatmap_days}
    if view == "timeline":
        ctx["events"] = build_timeline(current_user, limit=60)
    elif view == "favorites":
        ctx["rows"] = list_user_papers(current_user, limit=100, view="favorites")
    elif view == "read_later":
        ctx["rows"] = list_user_papers(current_user, limit=100, view="read_later")
    elif view == "notes":
        ctx["notes"] = list_all_notes(current_user, limit=100)
    elif view == "hidden":
        ctx["rows"] = list_user_papers(current_user, limit=100, view="dismissed")

    return render_template("library/index.html", **ctx)


def _parse_date(value: str | None):
    """Parse a YYYY-MM-DD query param to a datetime, or None on empty/invalid."""
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return None


@library_bp.route("/search")
@login_required
def search():
    """Filtered, paginated search over the user's own library."""
    from datetime import timedelta

    q = (request.args.get("q") or "").strip()
    source = (request.args.get("source") or "").strip() or None
    has_notes = request.args.get("has_notes") == "1"
    date_from = _parse_date(request.args.get("from"))
    date_to = _parse_date(request.args.get("to"))
    # Make the "to" date inclusive of the whole day.
    if date_to is not None:
        date_to = date_to + timedelta(days=1) - timedelta(seconds=1)
    page = request.args.get("page", 1, type=int)

    query = search_user_papers_query(
        current_user,
        q=q,
        source=source,
        date_from=date_from,
        date_to=date_to,
        has_notes=has_notes,
    )
    results = query.paginate(page=page, per_page=20, error_out=False)

    return render_template(
        "library/search.html",
        results=results,
        sources=distinct_user_sources(current_user),
        filters={
            "q": q,
            "source": source or "",
            "from": request.args.get("from", ""),
            "to": request.args.get("to", ""),
            "has_notes": has_notes,
        },
    )


@library_bp.route("/export.<fmt>")
@login_required
def export(fmt: str):
    """Bulk-export the user's library as BibTeX (.bib) or CSV (.csv).

    `?view=all|favorites|read_later` picks the slice (default: all). Only the
    caller's own papers are ever exported — list_user_papers is user-scoped.
    """
    from flask import Response, abort

    if fmt not in ("bib", "csv"):
        abort(404)
    view = request.args.get("view", "all")
    service_view = _EXPORT_VIEWS.get(view)
    if service_view is None:
        abort(404)

    rows = list_user_papers(current_user, limit=_EXPORT_LIMIT, view=service_view)

    if fmt == "bib":
        body = papers_to_bibtex(rows)
        mimetype = "application/x-bibtex"
    else:
        body = papers_to_csv(rows)
        mimetype = "text/csv"

    filename = f"scrapemind-{view}.{fmt}"
    return Response(
        body,
        mimetype=f"{mimetype}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


def _get_own_collection_or_404(collection_id: int):
    coll = cs.get_collection(current_user, collection_id)
    if coll is None:
        abort(404)
    return coll


@library_bp.route("/collections")
@login_required
def collections():
    return render_template(
        "library/collections.html", collections=cs.list_collections(current_user)
    )


@library_bp.route("/collections", methods=["POST"])
@login_required
def create_collection():
    coll, err = cs.create_collection(
        current_user, request.form.get("name", ""), request.form.get("description", "")
    )
    if err:
        flash(_(err), "danger")
        return redirect(url_for("library.collections"))
    log_action("collection.created", entity_type="collection", entity_id=str(coll.id))
    flash(_("Collection created."), "success")
    return redirect(url_for("library.collection_detail", collection_id=coll.id))


@library_bp.route("/collections/<int:collection_id>")
@login_required
def collection_detail(collection_id: int):
    coll = _get_own_collection_or_404(collection_id)
    # cs.list_papers_for_display (not the bare `coll.papers` relationship)
    # eager-loads Paper.video_summary — this page renders scrape/_paper_card.html
    # for every row, and that N+1 would otherwise be one query per card.
    return render_template(
        "library/collection_detail.html", collection=coll, rows=cs.list_papers_for_display(coll)
    )


@library_bp.route("/collections/<int:collection_id>/rename", methods=["POST"])
@login_required
def rename_collection(collection_id: int):
    coll = _get_own_collection_or_404(collection_id)
    ok, err = cs.rename_collection(
        coll, request.form.get("name", ""), request.form.get("description", "")
    )
    flash(_(err) if err else _("Collection updated."), "danger" if err else "success")
    return redirect(url_for("library.collection_detail", collection_id=coll.id))


@library_bp.route("/collections/<int:collection_id>/delete", methods=["POST"])
@login_required
def delete_collection(collection_id: int):
    coll = _get_own_collection_or_404(collection_id)
    cs.delete_collection(coll)
    log_action("collection.deleted", entity_type="collection", entity_id=str(collection_id))
    flash(_("Collection deleted."), "success")
    return redirect(url_for("library.collections"))


@library_bp.route("/collections/<int:collection_id>/add/<int:user_paper_id>", methods=["POST"])
@login_required
def add_to_collection(collection_id: int, user_paper_id: int):
    coll = _get_own_collection_or_404(collection_id)
    ok, err = cs.add_paper(coll, current_user, user_paper_id)
    if not ok:
        abort(404)  # the paper isn't the caller's
    if _is_htmx():
        return _collection_menu(user_paper_id)
    flash(_("Added to collection."), "success")
    return redirect(request.referrer or url_for("library.collection_detail", collection_id=coll.id))


@library_bp.route("/collections/<int:collection_id>/remove/<int:user_paper_id>", methods=["POST"])
@login_required
def remove_from_collection(collection_id: int, user_paper_id: int):
    coll = _get_own_collection_or_404(collection_id)
    cs.remove_paper(coll, user_paper_id)
    if _is_htmx():
        return _collection_menu(user_paper_id)
    flash(_("Removed from collection."), "success")
    return redirect(request.referrer or url_for("library.collection_detail", collection_id=coll.id))


@library_bp.route("/paper/<int:user_paper_id>/collections-menu")
@login_required
def collections_menu(user_paper_id: int):
    """HTMX partial: the 'add to collection' checkbox menu for one paper."""
    return _collection_menu(user_paper_id)


@library_bp.route("/collections/<int:collection_id>/share", methods=["POST"])
@login_required
def share_collection(collection_id: int):
    import uuid

    coll = _get_own_collection_or_404(collection_id)
    if not coll.share_token:
        coll.share_token = uuid.uuid4().hex
    coll.is_public = not coll.is_public
    db.session.commit()
    log_action("collection.shared", entity_type="collection", entity_id=str(coll.id))
    flash(_("Collection sharing updated."), "success")
    return redirect(url_for("library.collection_detail", collection_id=coll.id))


@library_bp.route("/c/<share_token>")
def public_collection(share_token: str):
    coll = Collection.query.filter_by(
        share_token=share_token, is_public=True, deleted_at=None
    ).first_or_404()
    return render_template("library/public_collection.html", collection=coll, rows=coll.papers)


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _collection_menu(user_paper_id: int):
    return render_template(
        "library/_collection_menu.html",
        collections=cs.list_collections(current_user),
        member_ids=cs.collection_ids_for_paper(current_user, user_paper_id),
        user_paper_id=user_paper_id,
    )


@library_bp.route("/collections/<int:collection_id>/export.<fmt>")
@login_required
def export_collection(collection_id: int, fmt: str):
    from flask import Response

    if fmt not in ("bib", "csv"):
        abort(404)
    coll = _get_own_collection_or_404(collection_id)
    rows = coll.papers
    body = papers_to_bibtex(rows) if fmt == "bib" else papers_to_csv(rows)
    mimetype = "application/x-bibtex" if fmt == "bib" else "text/csv"
    # Slugify the collection name for the filename.
    slug = "".join(c if c.isalnum() else "-" for c in coll.name.lower()).strip("-") or "collection"
    return Response(
        body,
        mimetype=f"{mimetype}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}.{fmt}"'},
    )
