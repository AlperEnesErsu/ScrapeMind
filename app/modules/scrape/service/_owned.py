"""Ownership-checked CRUD helpers shared by the per-user subscription modules.

Feeds, YouTube channels, web pages and Bluesky accounts are all "a row owned by
one user that can be listed, fetched, counted, removed and paused". Each module
keeps its own public `list_user_*` / `get_user_*` / ... functions (callers and
tests import those) and delegates the identical query and commit logic here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db


def list_owned(model: Any, user: User) -> list:
    """Every row of `model` this user owns, newest first."""
    return model.query.filter_by(user_id=user.id).order_by(desc(model.created_at)).all()


def get_owned(model: Any, user: User, row_id: int):
    """Fetch a row only if it belongs to this user (the ownership guard)."""
    return model.query.filter_by(id=row_id, user_id=user.id).first()


def count_owned(model: Any, user: User) -> int:
    return model.query.filter_by(user_id=user.id).count()


def remove_owned(model: Any, user: User, row_id: int) -> bool:
    row = get_owned(model, user, row_id)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def toggle_owned(
    model: Any, user: User, row_id: int, *, on_resume: Callable[[Any], None] | None = None
) -> bool | None:
    """Flip a row's `active` flag; returns the new value, or None if the row
    doesn't exist / isn't owned by this user (caller should 404).

    `on_resume` runs when the row becomes active again (e.g. to drop stale
    conditional-GET validators).
    """
    row = get_owned(model, user, row_id)
    if row is None:
        return None
    row.active = not row.active
    if row.active and on_resume is not None:
        on_resume(row)
    db.session.commit()
    return row.active


def clear_validators(row: Any) -> None:
    """Resuming a paused feed/page: it kept publishing while muted, and replaying
    a months-old etag would 304 past the whole gap."""
    row.etag = None
    row.last_modified = None
