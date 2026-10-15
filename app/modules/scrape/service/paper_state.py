"""Favorites, read-later, dismissal and notes on a paper."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import (
    PaperNote,
    UserPaper,
)

logger = structlog.get_logger()


def count_all_notes(user: User) -> int:
    from sqlalchemy import func

    return (
        db.session.query(func.count(PaperNote.id))
        .join(UserPaper, PaperNote.user_paper_id == UserPaper.id)
        .filter(UserPaper.user_id == user.id)
        .scalar()
        or 0
    )


def get_user_paper(user: User, user_paper_id: int) -> UserPaper | None:
    """Fetch a UserPaper that belongs to this user. Returns None on miss or
    ownership mismatch — callers should treat both the same way."""
    return UserPaper.query.filter_by(id=user_paper_id, user_id=user.id).first()


# Per-user paper state — favorites, dismiss, mark seen


def set_favorite(link: UserPaper, value: bool) -> bool:
    """Set the favorite flag to an explicit value. Idempotent — this is the
    primitive the API wants, since a retried request must not flip state
    back. Returns the new value."""
    link.is_favorite = bool(value)
    db.session.commit()
    return link.is_favorite


def toggle_favorite(link: UserPaper) -> bool:
    """Flip the favorite flag. Returns the new value so the caller can pick
    the right flash/UI state without re-querying."""
    return set_favorite(link, not link.is_favorite)


def set_read_later(link: UserPaper, value: bool) -> bool:
    """Set the read-later flag explicitly. See set_favorite."""
    link.read_later = bool(value)
    db.session.commit()
    return link.read_later


def toggle_read_later(link: UserPaper) -> bool:
    """Flip the read later (bookmark) flag."""
    return set_read_later(link, not link.read_later)


def set_dismissed(link: UserPaper, dismissed: bool) -> None:
    link.dismissed_at = datetime.now(UTC) if dismissed else None
    db.session.commit()


def mark_seen(link: UserPaper) -> None:
    if link.seen_at is None:
        link.seen_at = datetime.now(UTC)
        db.session.commit()


# Notes

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


def edit_note(note: PaperNote, body: str, tag: str | None = None) -> bool:
    """In-place note update. Empty body is a no-op (returns False) — match
    the add_note contract so callers can re-use validation logic."""
    body = (body or "").strip()
    if not body:
        return False
    tag = (tag or "").strip().lower() or None
    if tag not in ALLOWED_NOTE_TAGS:
        tag = None
    note.body = body
    note.tag = tag
    db.session.commit()
    return True


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
