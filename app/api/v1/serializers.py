"""Model → JSON-safe dict helpers for the v1 API.

Kept deliberately explicit (no automatic column dumping) so we never leak a
sensitive column — password_hash, totp_secret, recovery codes — by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.models.user import User
from app.modules.scrape.models import Paper, PaperNote, UserPaper


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def user_to_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "avatar_url": user.avatar_url,
        "locale": user.locale,
        "timezone": user.timezone,
        "is_superuser": user.is_superuser,
        "totp_enabled": user.is_totp_enabled,
        "created_at": _iso(user.created_at),
    }


def paper_to_dict(paper: Paper) -> dict[str, Any]:
    return {
        "id": paper.id,
        "source": paper.source,
        "external_id": paper.external_id,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": paper.authors or [],
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "categories": paper.categories or [],
        "published_at": _iso(paper.published_at),
    }


def user_paper_to_dict(up: UserPaper) -> dict[str, Any]:
    """A paper as surfaced to a user, merged with that user's per-paper state."""
    data = paper_to_dict(up.paper)
    data.update(
        {
            "user_paper_id": up.id,
            "is_favorite": up.is_favorite,
            "read_later": up.read_later,
            "matched_keyword": up.matched_keyword,
            "seen_at": _iso(up.seen_at),
            "dismissed_at": _iso(up.dismissed_at),
        }
    )
    return data


def note_to_dict(note: PaperNote) -> dict[str, Any]:
    return {
        "id": note.id,
        "user_paper_id": note.user_paper_id,
        "body": note.body,
        "tag": note.tag,
        "created_at": _iso(note.created_at),
        "updated_at": _iso(note.updated_at),
    }
