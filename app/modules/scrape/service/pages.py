"""Custom user web pages."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from flask import current_app
from flask_babel import lazy_gettext as _l
from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import (
    Paper,
    UserPage,
)
from app.modules.scrape.net_guard import is_public_http_url
from app.modules.scrape.service.feeds import _normalize_feed_url
from app.modules.scrape.service.papers import upsert_paper_status

logger = structlog.get_logger()


PAGE_CAP_MESSAGE = _l("Page limit reached. Remove one before adding another.")


def list_user_pages(user: User) -> list[UserPage]:
    """Every custom web page this user tracks, in stable order (newest first)."""
    return UserPage.query.filter_by(user_id=user.id).order_by(desc(UserPage.created_at)).all()


def get_user_page(user: User, page_id: int) -> UserPage | None:
    """Read one page row, asserting ownership."""
    return UserPage.query.filter_by(id=page_id, user_id=user.id).first()


def count_user_pages(user: User) -> int:
    return UserPage.query.filter_by(user_id=user.id).count()


def add_user_page(
    user: User, url: str, label: str | None = None, selector: str | None = None
) -> tuple[UserPage | None, str | None]:
    """Validate + normalize the URL, verify against robots.txt and SSRF guards,
    discover items via web_source.discover ladder, and auto-fill label.
    Returns (page, None) on success or (None, error_message) on failure.
    """
    from app.modules.scrape.sources.web_source import discover

    normalized = _normalize_feed_url(url)
    if normalized is None:
        return None, "Please enter a valid page URL (starting with http:// or https://)."

    clean_selector = (selector or "").strip()[:256] or None

    existing = UserPage.query.filter_by(user_id=user.id, url=normalized).first()
    if existing is not None:
        if not existing.active:
            existing.active = True
            existing.etag = None
            existing.last_modified = None
            if clean_selector:
                existing.selector = clean_selector
            db.session.commit()
        return existing, None

    max_pages = current_app.config.get("MAX_USER_PAGES", 30)
    if count_user_pages(user) >= max_pages:
        logger.info("user_page_cap_reached", user_id=user.id, cap=max_pages)
        return None, PAGE_CAP_MESSAGE

    allow_private = current_app.config.get("FEED_ALLOW_PRIVATE_HOSTS", False)
    ok, guard_error = is_public_http_url(normalized, allow_private=allow_private)
    if not ok:
        return None, guard_error

    result = discover(normalized, selector=clean_selector)
    if result.status == "robots_denied":
        return None, f"Website robots.txt denied access: {result.detail or 'Not allowed'}"
    if result.status != "ok" or not result.payloads:
        logger.warning(
            "user_page_validate_failed",
            user_id=user.id,
            url=normalized,
            status=result.status,
            detail=result.detail,
        )
        return (
            None,
            result.detail
            or "Could not extract any items from that page — try naming a CSS selector.",
        )

    clean_label = (label or "").strip()[:128] or None
    if not clean_label:
        clean_label = (result.title or "").strip()[:128] or None

    row = UserPage(
        user_id=user.id,
        url=normalized,
        label=clean_label,
        mode=result.mode,
        selector=clean_selector,
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    logger.info(
        "user_page_added", user_id=user.id, page_id=row.id, url=normalized, mode=result.mode
    )
    return row, None


def remove_user_page(user: User, page_id: int) -> bool:
    row = get_user_page(user, page_id)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def toggle_user_page(user: User, page_id: int) -> bool | None:
    """Flip a page's active flag. Returns the new value, or None if not found."""
    row = get_user_page(user, page_id)
    if row is None:
        return None
    row.active = not row.active
    if row.active:
        row.etag = None
        row.last_modified = None
    db.session.commit()
    return row.active


def ingest_user_pages(user: User) -> tuple[dict, list[Paper]]:
    """Fetch this user's active custom web pages and upsert new Papers
    (`source="user_page"`, `kind="news"`).

    Returns `(summary, touched_papers)`.
    """
    from app.modules.scrape.sources.web_source import discover

    pages = [p for p in list_user_pages(user) if p.active]
    if not pages:
        return {"hits": 0, "new": 0, "not_modified": 0}, []

    hits = 0
    new = 0
    not_modified = 0
    touched: list[Paper] = []
    for p in pages:
        try:
            result = discover(
                p.url,
                etag=p.etag,
                last_modified=p.last_modified,
                mode=p.mode,
                selector=p.selector,
            )
        except Exception:
            logger.exception("user_page_fetch_failed", user_id=user.id, page_id=p.id)
            continue

        if result.status == "not_modified":
            not_modified += 1
            continue
        if result.status != "ok":
            logger.warning(
                "user_page_ingest_bad_status",
                user_id=user.id,
                page_id=p.id,
                status=result.status,
            )
            continue

        hits += len(result.payloads)
        for payload in result.payloads:
            paper, created = upsert_paper_status(payload)
            touched.append(paper)
            if created:
                new += 1

        p.etag = result.etag
        p.last_modified = result.last_modified
        p.last_scraped_at = datetime.now(UTC)

    db.session.commit()
    return {"hits": hits, "new": new, "not_modified": not_modified}, touched
