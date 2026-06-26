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

from flask import Blueprint, render_template, request
from flask_login import current_user, login_required

from app.modules.scrape.service import (
    build_timeline,
    count_all_notes,
    count_user_papers,
    list_all_notes,
    list_user_papers,
)

library_bp = Blueprint("library", __name__, template_folder="templates")

_VALID_VIEWS = {"timeline", "favorites", "notes", "hidden"}


@library_bp.route("/")
@login_required
def index():
    view = request.args.get("view", "timeline")
    if view not in _VALID_VIEWS:
        view = "timeline"

    # Counts power the tab badges — SQL count, not Python len(materialized).
    counts = {
        "favorites": count_user_papers(current_user, view="favorites"),
        "notes": count_all_notes(current_user),
        "hidden": count_user_papers(current_user, view="dismissed"),
    }

    ctx: dict = {"view": view, "counts": counts}
    if view == "timeline":
        ctx["events"] = build_timeline(current_user, limit=60)
    elif view == "favorites":
        ctx["rows"] = list_user_papers(current_user, limit=100, view="favorites")
    elif view == "notes":
        ctx["notes"] = list_all_notes(current_user, limit=100)
    elif view == "hidden":
        ctx["rows"] = list_user_papers(current_user, limit=100, view="dismissed")

    return render_template("library/index.html", **ctx)
