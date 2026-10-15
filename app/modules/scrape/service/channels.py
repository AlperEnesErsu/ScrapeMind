"""Custom user YouTube channels."""

from __future__ import annotations

import structlog
from flask import current_app
from flask_babel import lazy_gettext as _l
from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import (
    UserChannel,
)
from app.modules.scrape.service.papers import link_user_paper, upsert_paper_status

logger = structlog.get_logger()


# Custom user YouTube channels — the twin of the custom-RSS-feed feature
# above, subscription-based the same way. CRUD first, `ingest_user_channels`
# (the Celery-driven ingestion, chaining transcript fetch + summarization)
# below it — see `app/tasks/channel_tasks.py` for the task that calls it.

#: Shown when a user tries to add channel number max_user_channels() + 1. Kept
#: as a module constant so the route, the template and the tests agree on it.
CHANNEL_CAP_MESSAGE = _l("Channel limit reached. Remove one before adding another.")


def max_user_channels() -> int:
    """The effective admin-set channel cap.

    Read from the `max_user_channels` system setting on every call, on
    purpose — an admin's change on `/settings/system` is live on the very
    next request, not just after a restart. Falls back to
    `MAX_USER_CHANNELS` (config/env) when no row exists yet.

    The setting lives in a JSON column an admin edits by hand, so it isn't
    trusted to already be an int: a garbage value (`"abc"`, `None`, a
    negative number) falls back to the config default / clamps to 0 rather
    than blowing up the add-channel flow.
    """
    from app.core.settings.service import get_system_setting

    fallback = current_app.config.get("MAX_USER_CHANNELS", 10)
    value = get_system_setting("max_user_channels", fallback)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(value, 0)


def list_user_channels(user: User) -> list[UserChannel]:
    return UserChannel.query.filter_by(user_id=user.id).order_by(desc(UserChannel.created_at)).all()


def get_user_channel(user: User, channel_pk: int) -> UserChannel | None:
    """Fetch a UserChannel only if it belongs to this user — same ownership
    guard as get_user_feed/get_note_for_user."""
    return UserChannel.query.filter_by(id=channel_pk, user_id=user.id).first()


def count_user_channels(user: User) -> int:
    return UserChannel.query.filter_by(user_id=user.id).count()


def add_user_channel(
    user: User, raw: str, label: str | None = None
) -> tuple[UserChannel | None, str | None]:
    """Resolve + register a YouTube channel subscription. Returns
    (channel, None) on success or (None, error_message) on any
    validation/resolution failure — never raises for bad user input.

    Step order mirrors add_user_feed:
      1. blank input is rejected before any resolution attempt
      2. resolve_channel(raw) does the actual work — SSRF guard, page/handle
         lookup and a live validating fetch of the channel's own RSS feed.
         Its error message is already plain, user-facing English, so it's
         returned as-is rather than repeating those checks here.
      3. re-adding an already-subscribed channel just reactivates it
         (if paused) instead of creating a duplicate row, and — like
         add_user_feed — consumes no cap slot to do so.
      4. only a genuinely new subscription is checked against the cap.
    """
    from app.modules.scrape.sources.youtube_channel_source import resolve_channel

    raw = (raw or "").strip()
    if not raw:
        return None, _l("Please enter a YouTube channel URL or @handle.")

    resolved, error = resolve_channel(raw)
    if resolved is None:
        return None, error

    existing = UserChannel.query.filter_by(
        user_id=user.id, channel_id=resolved["channel_id"]
    ).first()
    if existing is not None:
        if not existing.active:
            existing.active = True
            db.session.commit()
        return existing, None

    cap = max_user_channels()
    if count_user_channels(user) >= cap:
        logger.info("user_channel_cap_reached", user_id=user.id, cap=cap)
        return None, CHANNEL_CAP_MESSAGE

    clean_label = (label or "").strip()[:200] or None
    if not clean_label:
        clean_label = (resolved.get("title") or "")[:200] or None

    row = UserChannel(
        user_id=user.id,
        channel_id=resolved["channel_id"],
        title=clean_label,
        url=resolved["url"],
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    logger.info("user_channel_added", user_id=user.id, channel_pk=row.id, channel_id=row.channel_id)
    return row, None


def remove_user_channel(user: User, channel_pk: int) -> bool:
    row = get_user_channel(user, channel_pk)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def toggle_user_channel(user: User, channel_pk: int) -> bool | None:
    """Flip a channel's active flag. Returns the new value, or None if the
    channel doesn't exist / isn't owned by this user (caller should 404)."""
    row = get_user_channel(user, channel_pk)
    if row is None:
        return None
    row.active = not row.active
    db.session.commit()
    return row.active


def ingest_user_channels(user: User) -> tuple[dict, list[int]]:
    """Fetch this user's active YouTube channel subscriptions and upsert new
    Papers (`source="youtube_channel"`, `kind="video"`) — the channel
    counterpart of `ingest_user_feeds` above, called from
    `channel_tasks.ingest_for_user`.

    Unlike the RSS feed path (`UserFeed`/`ingest_user_feeds`), which stores an
    etag/last_modified on the row but never sends it back on the next fetch,
    this one round-trips them: `fetch_channel_videos` gets the row's stored
    values and a `not_modified` response short-circuits that channel at zero
    parsing/upsert cost.

    In practice that branch is currently dead for YouTube specifically:
    `feeds/videos.xml` answers with `Cache-Control: max-age=900` and **no
    ETag and no Last-Modified**, so both columns stay NULL and every run
    re-downloads the ~15 entries. Measured, not assumed — don't "fix" the
    empty etag column, there is nothing to store. The plumbing is kept
    because it costs nothing, works the moment YouTube starts sending
    validators, and is the pattern the feed path still needs to adopt.

    Returns `(summary, new_paper_ids)`:
      * `summary` maps `channel_id -> count` for that channel this run — `-1`
        is the same "this one errored" sentinel `scrape_for_user`/
        `feeds.ingest_all` use, so `apply_scan_result` reads it unchanged.
      * `new_paper_ids` is every Paper id newly *created* this run (for
        `channel_tasks.ingest_for_user` to hand to `summarize_video`).
        "Newly created" is decided the same way `ingest_user_feeds` decides
        it: a pre-upsert existence check on `(source, external_id)`, not
        `link_user_paper`'s `created` flag. The two diverge when a video's
        Paper row already exists (another user subscribes to the same
        channel) but is new to *this* user's library — that case must not
        queue a second summarization job for a video someone already
        summarized.

    One broken channel must never abort the loop — per-channel try/except,
    same isolation contract as `ingest_user_feeds`'s per-feed try/except.
    """
    from app.modules.scrape.sources.youtube_channel_source import fetch_channel_videos

    channels = [c for c in list_user_channels(user) if c.active]
    if not channels:
        return {}, []

    summary: dict[str, int] = {}
    new_paper_ids: list[int] = []
    for ch in channels:
        try:
            payloads, status, etag, last_modified, _title = fetch_channel_videos(
                ch.channel_id, etag=ch.etag, last_modified=ch.last_modified
            )
        except Exception:  # noqa: BLE001 — one broken channel must not block the others
            logger.exception(
                "user_channel_ingest_failed", user_id=user.id, channel_id=ch.channel_id
            )
            summary[ch.channel_id] = -1
            continue

        if status == "not_modified":
            summary[ch.channel_id] = 0
            continue
        if status != "ok":
            logger.warning(
                "user_channel_ingest_bad_status",
                user_id=user.id,
                channel_id=ch.channel_id,
                status=status,
            )
            summary[ch.channel_id] = -1
            continue

        ch.etag = etag
        ch.last_modified = last_modified

        newest_published_at = ch.last_video_at
        for payload in payloads:
            paper, created = upsert_paper_status(payload)
            link_user_paper(user, paper, matched_keyword=None)
            if created:
                new_paper_ids.append(paper.id)
            if payload.published_at and (
                newest_published_at is None or payload.published_at > newest_published_at
            ):
                newest_published_at = payload.published_at

        if newest_published_at is not None:
            ch.last_video_at = newest_published_at

        db.session.commit()
        summary[ch.channel_id] = len(payloads)

    logger.info(
        "user_channels_ingest_done", user_id=user.id, summary=summary, new=len(new_paper_ids)
    )
    return summary, new_paper_ids
