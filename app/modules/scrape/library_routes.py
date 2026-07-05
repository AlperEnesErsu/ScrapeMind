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

_VALID_VIEWS = {"timeline", "favorites", "read_later", "notes", "hidden"}


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
