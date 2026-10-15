"""RSS feed relevance linking and custom user feeds."""

from __future__ import annotations

from urllib.parse import urlparse

import structlog
from flask import current_app
from flask_babel import lazy_gettext as _l
from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import (
    Paper,
    UserFeed,
    UserPaper,
)
from app.modules.scrape.net_guard import is_public_http_url
from app.modules.scrape.service.papers import link_user_paper, upsert_paper
from app.modules.scrape.service.source_prefs import list_user_source_prefs
from app.modules.scrape.sources.rss_source import fetch_feed_conditional

logger = structlog.get_logger()


#: Shown when a user tries to add feed number MAX_USER_FEEDS + 1. Kept as a
#: module constant so the route, the template and the tests agree on it. No
#: placeholder: the route passes it straight to `_()`, and the panel header
#: already shows the "42 / 50" count.
FEED_CAP_MESSAGE = _l("Feed limit reached. Remove one before adding another.")


# RSS feed relevance — per-user LLM scoring + linking (Faz 2 Bölüm D)
#
# Different flow from scrape_for_user above: feeds are fetched once globally
# by app/tasks/feed_tasks.py:ingest_all (see app/modules/scrape/sources/
# rss_source.py), landing as Paper rows with kind="news" that no user has
# been linked to yet. This function does the per-user half — score the
# unlinked backlog against the user's interests and link what clears the
# threshold — so it belongs to a separate task (feeds.link_for_user) rather
# than the nightly scrape_for_user/run_for_user pair.

FEED_LINK_CANDIDATE_LIMIT = 50  # DB-side cap before the LLM's own windowing


def link_relevant_feed_items(
    user: User, *, threshold: int = 60, extra_candidates: list[Paper] | None = None
) -> dict:
    """Score recently-ingested `kind="news"` Papers this user isn't linked to
    yet, and link everything scoring >= threshold (matched_keyword = the
    LLM's pick of the most relevant interest term).

    Skips entirely (no LLM call) when the user has muted every enabled feed
    source via `UserSource` — opt-out is per-feed (each feed is its own
    source key, see sources/rss_source.py:FEEDS), so muting one feed still
    lets the others score normally.

    `extra_candidates` (Faz 3 Bölüm D) lets a caller fold in Papers from
    outside the curated-feed DB query — specifically the just-fetched output
    of `ingest_user_feeds(user)`. Custom feeds all share `Paper.source ==
    "user_feed"` across every user who's added one, so there's no reliable
    column to filter "this user's custom feeds" by in a query; the caller
    already knows exactly which Papers its own feeds just produced, so it's
    both simpler and more precise to pass them in directly than to guess from
    the DB. These bypass the curated-feed mute check (a user's own added feed
    isn't governed by the curated `UserSource` rows) but are still deduped
    against `already_linked`.
    """
    from app.modules.scrape.ai_service import score_feed_relevance
    from app.modules.scrape.sources.rss_source import FEEDS

    feed_keys = {f["key"] for f in FEEDS}
    prefs = list_user_source_prefs(user)
    active_feed_keys = {k for k in feed_keys if prefs.get(k, True)}

    already_linked = {
        row[0]
        for row in db.session.query(UserPaper.paper_id).filter(UserPaper.user_id == user.id).all()
    }

    candidates: list[Paper] = []
    seen_ids: set[int] = set()
    if active_feed_keys:
        query = Paper.query.filter(Paper.kind == "news", Paper.source.in_(active_feed_keys))
        if already_linked:
            query = query.filter(~Paper.id.in_(already_linked))
        for paper in (
            query.order_by(desc(Paper.published_at), desc(Paper.created_at))
            .limit(FEED_LINK_CANDIDATE_LIMIT)
            .all()
        ):
            candidates.append(paper)
            seen_ids.add(paper.id)

    for paper in extra_candidates or []:
        if paper.id in already_linked or paper.id in seen_ids:
            continue
        candidates.append(paper)
        seen_ids.add(paper.id)

    if not candidates:
        if not active_feed_keys and not extra_candidates:
            logger.info("feed_link_skip_all_muted", user_id=user.id)
            return {"scored": 0, "linked": 0, "reason": "news_muted"}
        return {"scored": 0, "linked": 0, "reason": "no_candidates"}

    scores = score_feed_relevance(user, candidates)
    by_id = {p.id: p for p in candidates}
    linked = 0
    for entry in scores:
        if entry["score"] < threshold:
            continue
        paper = by_id.get(entry["paper_id"])
        if paper is None:
            continue
        _, created = link_user_paper(user, paper, matched_keyword=entry.get("matched_keyword"))
        if created:
            linked += 1
    logger.info("feed_link_done", user_id=user.id, scored=len(scores), linked=linked)
    return {"scored": len(scores), "linked": linked}


# Custom user RSS feeds (Faz 3 Bölüm D)


def _normalize_feed_url(raw: str | None) -> str | None:
    """Trim, default to https:// when no scheme was given, and reject
    anything that doesn't parse into a real http(s) URL. Returns None on
    anything unusable."""
    url = (raw or "").strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url


def list_user_feeds(user: User) -> list[UserFeed]:
    return UserFeed.query.filter_by(user_id=user.id).order_by(desc(UserFeed.created_at)).all()


def get_user_feed(user: User, feed_id: int) -> UserFeed | None:
    """Fetch a UserFeed only if it belongs to this user — same ownership
    guard as get_note_for_user/get_user_paper."""
    return UserFeed.query.filter_by(id=feed_id, user_id=user.id).first()


def count_user_feeds(user: User) -> int:
    return UserFeed.query.filter_by(user_id=user.id).count()


def add_user_feed(
    user: User, url: str, label: str | None = None
) -> tuple[UserFeed | None, str | None]:
    """Validate + normalize the URL, fetch it once to confirm it actually
    yields entries, and auto-fill a label from the feed's own title when none
    was given. Returns (feed, None) on success or (None, error_message) on any
    validation/parse failure — never raises for a bad URL, that's an expected
    user input error, not a bug.

    Two limits guard the server here, both of which exist because this is the
    one place a user hands us an address we then fetch ourselves:

      * `MAX_USER_FEEDS` — each active feed is one HTTP request per nightly
        run, so an unbounded list is an unbounded per-user cost.
      * the SSRF guard — a feed pointing at `http://127.0.0.1:6379/` or the
        cloud metadata endpoint would otherwise be fetched from inside the
        network perimeter (see `net_guard.is_public_http_url`).

    Idempotent per (user, url): re-adding an existing URL just re-activates
    it (if it had been paused) instead of creating a duplicate row. Re-adding
    is therefore allowed even at the cap — it consumes no new slot.
    """
    normalized = _normalize_feed_url(url)
    if normalized is None:
        return None, _l("Please enter a valid feed URL (starting with http:// or https://).")

    existing = UserFeed.query.filter_by(user_id=user.id, url=normalized).first()
    if existing is not None:
        if not existing.active:
            existing.active = True
            # Re-activating forgets the validators — see the note below. While
            # the feed was paused it kept publishing, and a stale etag would
            # 304 straight past everything it published in the meantime.
            existing.etag = None
            existing.last_modified = None
            db.session.commit()
        return existing, None

    max_feeds = current_app.config.get("MAX_USER_FEEDS", 50)
    if count_user_feeds(user) >= max_feeds:
        logger.info("user_feed_cap_reached", user_id=user.id, cap=max_feeds)
        return None, FEED_CAP_MESSAGE

    allow_private = current_app.config.get("FEED_ALLOW_PRIVATE_HOSTS", False)
    ok, guard_error = is_public_http_url(normalized, allow_private=allow_private)
    if not ok:
        return None, guard_error

    parsed_feed = fetch_feed_conditional({"key": "user_feed", "url": normalized})
    if parsed_feed.status != "ok" or not parsed_feed.payloads:
        logger.warning(
            "user_feed_validate_failed",
            user_id=user.id,
            url=normalized,
            status=parsed_feed.status,
        )
        return None, _l("Could not read that feed — check the URL and try again.")

    clean_label = (label or "").strip()[:128] or None
    if not clean_label:
        clean_label = parsed_feed.title[:128] if parsed_feed.title else None

    # The validators from this fetch are deliberately NOT stored. This request
    # validates the URL; it does not ingest anything. Persisting its etag would
    # make the first nightly `ingest_user_feeds` answer 304 and skip the very
    # items that proved the feed was readable — the feed would look permanently
    # empty. One full fetch on the first run is the price of that correctness.
    row = UserFeed(
        user_id=user.id,
        url=normalized,
        label=clean_label,
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    logger.info("user_feed_added", user_id=user.id, feed_id=row.id, url=normalized)
    return row, None


def remove_user_feed(user: User, feed_id: int) -> bool:
    row = get_user_feed(user, feed_id)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def toggle_user_feed(user: User, feed_id: int) -> bool | None:
    """Flip a feed's active flag. Returns the new value, or None if the feed
    doesn't exist / isn't owned by this user (caller should 404).

    Resuming a paused feed clears its conditional-GET validators: the feed kept
    publishing while it was muted, and replaying a months-old etag would 304
    past the whole gap. Pausing leaves them alone — they are about to be
    cleared on resume anyway, and a paused feed is never fetched.
    """
    row = get_user_feed(user, feed_id)
    if row is None:
        return None
    row.active = not row.active
    if row.active:
        row.etag = None
        row.last_modified = None
    db.session.commit()
    return row.active


def ingest_user_feeds(user: User) -> tuple[dict, list[Paper]]:
    """Fetch this user's active custom RSS feeds and upsert new Papers
    (`source="user_feed"`, `kind="news"`) — the per-user counterpart of
    `feed_tasks.ingest_all` for the curated feeds. Called from
    `feed_tasks.link_for_user` right before `link_relevant_feed_items`, per
    user, never globally (a custom feed only exists because one user added it).

    `source="user_feed"` is shared across every user's custom feed (any URL)
    — the normal (source, external_id) dedup in `upsert_paper` means two
    users adding the identical URL land on the same Paper rows.

    Conditional GET: the row's stored `etag`/`last_modified` are sent back on
    every fetch and a 304 short-circuits that feed at zero parse/upsert cost,
    the same round-trip `ingest_user_channels` does. A 304 feed contributes
    nothing to `touched` on purpose — its items were already scored on the run
    that first ingested them, and re-scoring an unchanged feed every night is
    exactly the LLM spend this is here to avoid.

    Returns (summary_dict, touched_papers) — `touched_papers` is every Paper
    this run's fetch produced (new AND already-existing rows re-fetched from
    the still-active feed), which the caller feeds into
    `link_relevant_feed_items(..., extra_candidates=touched_papers)` so this
    user's own custom-feed content is eligible for relevance scoring even
    though it isn't part of the curated-feed DB query.
    """
    from app.modules.scrape.sources.rss_source import fetch_feed_conditional

    feeds = [f for f in list_user_feeds(user) if f.active]
    if not feeds:
        return {"hits": 0, "new": 0, "not_modified": 0}, []

    hits = 0
    new = 0
    not_modified = 0
    touched: list[Paper] = []
    for f in feeds:
        try:
            result = fetch_feed_conditional(
                {"key": "user_feed", "url": f.url},
                etag=f.etag,
                last_modified=f.last_modified,
            )
        except Exception:  # noqa: BLE001 — one broken custom feed must not block the others
            logger.exception("user_feed_fetch_failed", user_id=user.id, feed_id=f.id)
            continue

        if result.status == "not_modified":
            not_modified += 1
            continue
        if result.status != "ok":
            logger.warning(
                "user_feed_ingest_bad_status",
                user_id=user.id,
                feed_id=f.id,
                status=result.status,
            )
            continue

        hits += len(result.payloads)
        for payload in result.payloads:
            existed = (
                Paper.query.filter_by(
                    source=payload.source, external_id=payload.external_id
                ).first()
                is not None
            )
            paper = upsert_paper(payload)
            touched.append(paper)
            if not existed:
                new += 1

        # Validators are stored only after the upserts land. If the loop above
        # raises, this feed keeps its old etag and the next run re-fetches in
        # full — the opposite order would 304 past items we never persisted.
        f.etag = result.etag
        f.last_modified = result.last_modified
        db.session.commit()
    logger.info(
        "user_feeds_ingest_done",
        user_id=user.id,
        hits=hits,
        new=new,
        not_modified=not_modified,
    )
    return {"hits": hits, "new": new, "not_modified": not_modified}, touched
