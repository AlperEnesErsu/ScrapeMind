"""Custom user Bluesky accounts."""

from __future__ import annotations

import structlog
from flask import current_app
from flask_babel import lazy_gettext as _l

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import (
    Paper,
    UserBluesky,
)
from app.modules.scrape.service._owned import (
    count_owned,
    get_owned,
    list_owned,
    remove_owned,
    toggle_owned,
)
from app.modules.scrape.service.papers import upsert_paper_status

logger = structlog.get_logger()


BLUESKY_CAP_MESSAGE = _l("Bluesky account limit reached. Remove one before adding another.")


def list_user_bluesky(user: User) -> list[UserBluesky]:
    """Every Bluesky account this user tracks, newest first."""
    return list_owned(UserBluesky, user)


def get_user_bluesky(user: User, bluesky_id: int) -> UserBluesky | None:
    """Read one Bluesky account row, asserting ownership."""
    return get_owned(UserBluesky, user, bluesky_id)


def count_user_bluesky(user: User) -> int:
    return count_owned(UserBluesky, user)


def add_user_bluesky(user: User, handle_or_url: str) -> tuple[UserBluesky | None, str | None]:
    """Validate and resolve a Bluesky account, saving it to UserBluesky.

    Returns (account, None) on success or (None, error_message) on failure.
    """
    from app.modules.scrape.sources.bluesky_source import resolve_account

    clean_input = (handle_or_url or "").strip()
    if not clean_input:
        return None, "Please enter a valid Bluesky handle or profile link."

    max_accounts = current_app.config.get("MAX_USER_BLUESKY", 20)
    if count_user_bluesky(user) >= max_accounts:
        logger.info("user_bluesky_cap_reached", user_id=user.id, cap=max_accounts)
        return None, BLUESKY_CAP_MESSAGE

    try:
        did, handle, display_name, avatar_url = resolve_account(clean_input)
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:
        logger.warning("user_bluesky_resolve_error", user_id=user.id, exc=str(exc))
        return None, "Could not verify that Bluesky account — please try again."

    existing = UserBluesky.query.filter_by(user_id=user.id, did=did).first()
    if existing is not None:
        if not existing.active:
            existing.active = True
            existing.handle = handle
            existing.display_name = display_name
            existing.avatar_url = avatar_url
            db.session.commit()
        return existing, None

    row = UserBluesky(
        user_id=user.id,
        did=did,
        handle=handle,
        display_name=display_name,
        avatar_url=avatar_url,
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    logger.info("user_bluesky_added", user_id=user.id, bluesky_id=row.id, handle=handle, did=did)
    return row, None


def remove_user_bluesky(user: User, bluesky_id: int) -> bool:
    return remove_owned(UserBluesky, user, bluesky_id)


def toggle_user_bluesky(user: User, bluesky_id: int) -> bool | None:
    """Flip a Bluesky account's active flag. Returns the new value, or None if not found."""
    return toggle_owned(UserBluesky, user, bluesky_id)


def ingest_user_bluesky(user: User) -> tuple[dict, list[Paper]]:
    """Fetch recent posts from this user's active Bluesky accounts and upsert Papers
    (`source="bluesky"`, `kind="social"`).

    Returns `(summary, touched_papers)`.
    """
    from app.modules.scrape.sources.bluesky_source import fetch_author_posts

    accounts = [a for a in list_user_bluesky(user) if a.active]
    if not accounts:
        return {"hits": 0, "new": 0}, []

    hits = 0
    new = 0
    touched: list[Paper] = []
    for a in accounts:
        try:
            payloads = fetch_author_posts(a.did or a.handle, since=a.last_post_at, limit=30)
        except Exception:
            logger.exception("user_bluesky_fetch_failed", user_id=user.id, bluesky_id=a.id)
            continue

        hits += len(payloads)
        max_published_at = a.last_post_at
        for payload in payloads:
            paper, created = upsert_paper_status(payload)
            touched.append(paper)
            if created:
                new += 1
            if payload.published_at and (
                max_published_at is None or payload.published_at > max_published_at
            ):
                max_published_at = payload.published_at

        a.last_post_at = max_published_at

    db.session.commit()
    return {"hits": hits, "new": new}, touched
