"""Persistence layer for scraped papers.

The service deduplicates incoming payloads against (source, external_id)
and keeps a per-user record of which keyword surfaced what — that's how
the "For you" dashboard becomes a personal feed instead of a firehose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db
from app.modules.academic.service import list_user_identifiers, list_user_keywords
from app.modules.scrape.models import Paper, PaperNote, UserPaper
from app.modules.scrape.sources.arxiv_source import PaperPayload
from app.modules.scrape.sources.arxiv_source import search as arxiv_search

logger = structlog.get_logger()


def upsert_paper(payload: PaperPayload | dict) -> Paper:
    """Insert a paper if we haven't seen it before, return the row either way."""
    data = payload.as_dict() if isinstance(payload, PaperPayload) else dict(payload)
    existing = Paper.query.filter_by(source=data["source"], external_id=data["external_id"]).first()
    if existing is not None:
        return existing
    paper = Paper(**data)
    db.session.add(paper)
    db.session.commit()
    return paper


def link_user_paper(
    user: User, paper: Paper, *, matched_keyword: str | None
) -> tuple[UserPaper, bool]:
    """Idempotent. Returns (link, created) — created=True only on first insert."""
    existing = UserPaper.query.filter_by(user_id=user.id, paper_id=paper.id).first()
    if existing is not None:
        return existing, False
    link = UserPaper(user_id=user.id, paper_id=paper.id, matched_keyword=matched_keyword)
    db.session.add(link)
    db.session.commit()
    return link, True


def _build_arxiv_query(keywords: list[str], orcid_values: list[str]) -> str:
    """Compose an arXiv search expression from user interests + identifiers."""
    parts: list[str] = []
    # Keywords go into the full-text search ("all:")
    for kw in keywords:
        parts.append(f'all:"{kw}"')
    # ORCID isn't supported by arXiv search directly; fall back to nothing for
    # now. (Later slices might match by author name strings from user_identifiers.)
    _ = orcid_values  # placeholder until author-name identifiers land
    return " OR ".join(parts)


def scrape_arxiv_for_user(user: User, *, max_results: int = 25) -> dict:
    """Run an arXiv search using this user's keywords; persist + link the
    results back to them. Returns a summary dict for the calling task."""
    keywords = [kw.value for kw in list_user_keywords(user)]
    orcid_idents = [
        i.value for i in list_user_identifiers(user, type_code="orcid") if i.is_verified
    ]
    if not keywords:
        logger.info("scrape_skip_no_keywords", user_id=user.id)
        return {"hits": 0, "linked": 0, "reason": "no_keywords"}

    query = _build_arxiv_query(keywords, orcid_idents)
    payloads = arxiv_search(query, max_results=max_results)

    linked = 0
    for payload in payloads:
        paper = upsert_paper(payload)
        # Best-effort keyword match: pick the first keyword whose tokens all
        # appear in the title — good enough for analytics, perfect for v1.
        matched_kw = next(
            (kw for kw in keywords if all(tok in paper.title.lower() for tok in kw.split())),
            keywords[0],
        )
        _, created = link_user_paper(user, paper, matched_keyword=matched_kw)
        if created:
            linked += 1
    logger.info("scrape_done", user_id=user.id, hits=len(payloads), linked=linked)
    return {"hits": len(payloads), "linked": linked, "query": query}


def list_user_papers(
    user: User,
    *,
    limit: int = 50,
    view: str = "discover",
) -> list[UserPaper]:
    """List a user's surfaced papers.

    Views:
        * "discover" — feed, hides dismissed rows
        * "favorites" — starred only
        * "dismissed" — hidden bin (recovery)
        * "all"      — everything, no filter
    """
    q = UserPaper.query.filter_by(user_id=user.id).join(Paper)
    if view == "discover":
        q = q.filter(UserPaper.dismissed_at.is_(None))
    elif view == "favorites":
        q = q.filter(UserPaper.is_favorite.is_(True), UserPaper.dismissed_at.is_(None))
    elif view == "dismissed":
        q = q.filter(UserPaper.dismissed_at.isnot(None))
    return q.order_by(desc(Paper.published_at), desc(UserPaper.created_at)).limit(limit).all()


def get_user_paper(user: User, user_paper_id: int) -> UserPaper | None:
    """Fetch a UserPaper that belongs to this user. Returns None on miss or
    ownership mismatch — callers should treat both the same way."""
    return UserPaper.query.filter_by(id=user_paper_id, user_id=user.id).first()


# ----------------------------------------------------------------------------
# Per-user paper state — favorites, dismiss, mark seen
# ----------------------------------------------------------------------------


def toggle_favorite(link: UserPaper) -> bool:
    """Flip the favorite flag. Returns the new value so the caller can pick
    the right flash/UI state without re-querying."""
    link.is_favorite = not link.is_favorite
    db.session.commit()
    return link.is_favorite


def set_dismissed(link: UserPaper, dismissed: bool) -> None:
    link.dismissed_at = datetime.now(UTC) if dismissed else None
    db.session.commit()


def mark_seen(link: UserPaper) -> None:
    if link.seen_at is None:
        link.seen_at = datetime.now(UTC)
        db.session.commit()


# ----------------------------------------------------------------------------
# Notes
# ----------------------------------------------------------------------------

ALLOWED_NOTE_TAGS = {"deney", "soru", "sonuç", "okuma", None, ""}


def add_note(link: UserPaper, body: str, tag: str | None = None) -> PaperNote | None:
    """Create a note on a UserPaper. Empty/whitespace-only bodies are
    rejected (returns None) — that's the only validation the service does."""
    body = (body or "").strip()
    if not body:
        return None
    tag = (tag or "").strip().lower() or None
    if tag not in ALLOWED_NOTE_TAGS:
        tag = None
    note = PaperNote(user_paper_id=link.id, body=body, tag=tag)
    db.session.add(note)
    db.session.commit()
    return note


def delete_note(note: PaperNote) -> None:
    db.session.delete(note)
    db.session.commit()


def get_note_for_user(user: User, note_id: int) -> PaperNote | None:
    """Fetch a note only if its parent UserPaper belongs to this user.

    Guards against /papers/notes/<other-user-id> drive-by deletes — we
    enforce ownership at the service layer so every caller is safe.
    """
    return (
        PaperNote.query.join(UserPaper, PaperNote.user_paper_id == UserPaper.id)
        .filter(PaperNote.id == note_id, UserPaper.user_id == user.id)
        .first()
    )


def list_all_notes(user: User, *, limit: int = 100) -> list[PaperNote]:
    """Every note this user has ever written, newest first. Used by the
    Library "Notes" tab to show notes across papers in one stream."""
    return (
        PaperNote.query.join(UserPaper, PaperNote.user_paper_id == UserPaper.id)
        .filter(UserPaper.user_id == user.id)
        .order_by(desc(PaperNote.created_at))
        .limit(limit)
        .all()
    )


# ----------------------------------------------------------------------------
# Timeline — merged activity stream for the Library page
# ----------------------------------------------------------------------------


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
KIND_FAVORITED = "favorited"
KIND_NOTE_ADDED = "note_added"
KIND_DISMISSED = "dismissed"
KIND_KEYWORD_ADDED = "keyword_added"


def build_timeline(user: User, *, limit: int = 40) -> list[TimelineEvent]:
    """Build a merged activity stream of recent events for this user.

    Sources, in priority order:
      * audit_logs    — scrape runs (manual + nightly fan-out)
      * paper_notes   — every note the user wrote
      * user_papers   — favorited (uses updated_at as a proxy when is_favorite)
                        + dismissed (dismissed_at) + newly surfaced (created_at)
      * user_keywords — when the user added a new research interest

    Each source query is capped by `limit` independently so a quiet week
    of one source doesn't crowd out another.
    """
    from urllib.parse import quote

    from app.core.models.audit import AuditLog
    from app.modules.academic.models import Keyword, UserKeyword

    events: list[TimelineEvent] = []

    # ---- Scrape runs (from audit) ----
    audit_runs = (
        AuditLog.query.filter(
            AuditLog.user_id == user.id,
            AuditLog.action.in_(["scrape.manual_run", "scrape.auto_run"]),
        )
        .order_by(desc(AuditLog.created_at))
        .limit(limit)
        .all()
    )
    for a in audit_runs:
        events.append(
            TimelineEvent(
                when=a.created_at,
                kind=KIND_SCRAPE,
                title="Otomatik tarama" if a.action == "scrape.auto_run" else "Manuel tarama",
                detail=None,
                badge=None,
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
