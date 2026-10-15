"""Per-user activity timeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import desc

from app.core.models.user import User
from app.modules.scrape.models import (
    ScanRun,
    UserPaper,
)
from app.modules.scrape.service.paper_state import list_all_notes

logger = structlog.get_logger()


# Timeline — merged activity stream for the Library page


@dataclass(frozen=True)
class TimelineEvent:
    """A single row on the Library timeline. Multiple source tables
    (user_papers, paper_notes, audit_logs, user_keywords) are folded into
    this one shape so the template just renders a list."""

    when: datetime
    kind: str  # see KIND_* constants below
    title: str
    detail: str | None = None
    badge: str | None = None  # short status pill (e.g. "+12 new")
    link_url: str | None = None
    tag: str | None = None  # for note events


KIND_SCRAPE = "scrape_run"


KIND_PATENT_SCAN = "patent_run"


KIND_AUTHOR_SCAN = "author_run"


KIND_FAVORITED = "favorited"


KIND_NOTE_ADDED = "note_added"


KIND_DISMISSED = "dismissed"


KIND_KEYWORD_ADDED = "keyword_added"


def build_timeline(user: User, *, limit: int = 40) -> list[TimelineEvent]:
    """Build a merged activity stream of recent events for this user.

    Sources, in priority order:
      * scan_runs     — scrape runs (manual + nightly fan-out)
      * paper_notes   — every note the user wrote
      * user_papers   — favorited (uses updated_at as a proxy when is_favorite)
                        + dismissed (dismissed_at) + newly surfaced (created_at)
      * user_keywords — when the user added a new research interest

    Each source query is capped by `limit` independently so a quiet week
    of one source doesn't crowd out another.
    """
    from urllib.parse import quote

    from app.modules.academic.models import Keyword, UserKeyword

    events: list[TimelineEvent] = []

    # ---- Scrape runs ----
    # Read from scan_runs, not audit_logs. The audit path only ever recorded
    # manual runs: it also looked for a "scrape.auto_run" action that nothing
    # wrote, so nightly runs never appeared here at all.
    runs = (
        ScanRun.query.filter(
            ScanRun.user_id == user.id,
            ScanRun.kind == "scrape",
            ScanRun.status.in_(("ok", "partial", "error")),
        )
        .order_by(desc(ScanRun.started_at))
        .limit(limit)
        .all()
    )
    for r in runs:
        events.append(
            TimelineEvent(
                when=r.finished_at or r.started_at,
                kind=KIND_SCRAPE,
                title="Manuel tarama" if r.trigger == "manual" else "Otomatik tarama",
                detail=None,
                badge=f"+{r.new_items}" if r.new_items else None,
            )
        )

    # ---- Patent scans ----
    # Their own event kind rather than folding into the scrape runs above: a
    # patent run is a separate ScanRun (see `scrape_patents_for_user`), and
    # showing both as "tarama" would make two rows look like a duplicate.
    patent_runs = (
        ScanRun.query.filter(
            ScanRun.user_id == user.id,
            ScanRun.kind == "patents",
            ScanRun.status.in_(("ok", "partial", "error")),
        )
        .order_by(desc(ScanRun.started_at))
        .limit(limit)
        .all()
    )
    for r in patent_runs:
        events.append(
            TimelineEvent(
                when=r.finished_at or r.started_at,
                kind=KIND_PATENT_SCAN,
                title="Manuel patent taraması" if r.trigger == "manual" else "Patent taraması",
                detail=None,
                badge=f"+{r.new_items}" if r.new_items else None,
            )
        )

    # ---- Author scans ----
    author_runs = (
        ScanRun.query.filter(
            ScanRun.user_id == user.id,
            ScanRun.kind == "authors",
            ScanRun.status.in_(("ok", "partial", "error")),
        )
        .order_by(desc(ScanRun.started_at))
        .limit(limit)
        .all()
    )
    for r in author_runs:
        events.append(
            TimelineEvent(
                when=r.finished_at or r.started_at,
                kind=KIND_AUTHOR_SCAN,
                title="Takip edilen yazarlar",
                detail=None,
                badge=f"+{r.new_items}" if r.new_items else None,
            )
        )

    # ---- Note events ----
    notes = list_all_notes(user, limit=limit)
    for n in notes:
        link = n.user_paper
        # Body preview — first ~70 chars on one line
        preview = (n.body or "").replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:78] + "…"
        events.append(
            TimelineEvent(
                when=n.created_at,
                kind=KIND_NOTE_ADDED,
                title=link.paper.title,
                detail=preview,
                tag=n.tag,
                link_url=f"/papers/{link.id}",
            )
        )

    # ---- Favorites + dismissals ----
    fav_rows = (
        UserPaper.query.filter(UserPaper.user_id == user.id, UserPaper.is_favorite.is_(True))
        .order_by(desc(UserPaper.updated_at))
        .limit(limit)
        .all()
    )
    for r in fav_rows:
        # updated_at is None when the row was inserted as favorite at scrape
        # time (it never had a separate flip). Fall back to created_at.
        when = r.updated_at or r.created_at
        events.append(
            TimelineEvent(
                when=when,
                kind=KIND_FAVORITED,
                title=r.paper.title,
                detail=r.matched_keyword,
                link_url=f"/papers/{r.id}",
            )
        )

    dis_rows = (
        UserPaper.query.filter(UserPaper.user_id == user.id, UserPaper.dismissed_at.isnot(None))
        .order_by(desc(UserPaper.dismissed_at))
        .limit(limit)
        .all()
    )
    for r in dis_rows:
        events.append(
            TimelineEvent(
                when=r.dismissed_at,
                kind=KIND_DISMISSED,
                title=r.paper.title,
                detail=r.matched_keyword,
                link_url=f"/papers/{r.id}",
            )
        )

    # ---- Keyword adds ----
    kw_links = (
        UserKeyword.query.filter_by(user_id=user.id)
        .join(Keyword)
        .order_by(desc(UserKeyword.created_at))
        .limit(limit)
        .all()
    )
    for ul in kw_links:
        events.append(
            TimelineEvent(
                when=ul.created_at,
                kind=KIND_KEYWORD_ADDED,
                title=ul.keyword.value,
                detail=None,
                link_url=f"/papers/?q={quote(ul.keyword.value)}",
            )
        )

    events.sort(key=lambda e: e.when, reverse=True)
    return events[:limit]
