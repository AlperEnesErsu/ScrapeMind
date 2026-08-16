"""RSS feed tasks — global ingestion + per-user relevance linking.

Two distinct steps, two distinct fan-out shapes:

  * `ingest_all` — runs ONCE for the whole deployment (feeds are the same
    content for every user), fetching each enabled feed and upserting new
    `Paper` rows (kind="news"). No user in sight here at all.
  * `link_for_all_users` / `link_for_user` — mirrors the `scrape_tasks.py` /
    `digest_tasks.py` fan-out pattern: one task per active user, scoring the
    just-ingested backlog against that user's interests and linking what
    clears the threshold (see `service.link_relevant_feed_items`).

Beat runs these in order: `feeds.ingest_all` (~02:45) -> the existing nightly
`scrape.run_for_all_users` (~03:15) -> `feeds.link_for_all_users` (~03:45) ->
`digest.run_for_all_users` (~04:00), so by the time the daily digest reads a
user's newly-linked papers, the relevant news items are already there.
"""

from __future__ import annotations

import structlog

from app.core.models.user import User
from app.tasks import celery_app
from app.tasks.fanout import fan_out

logger = structlog.get_logger()


#: System-settings key holding the curated feeds' conditional-GET validators,
#: shaped `{feed_key: {"etag": str|None, "last_modified": str|None}}`.
#:
#: Curated feeds are module constants in `rss_source.FEEDS`, not DB rows, so
#: unlike `UserFeed`/`UserChannel` they have nowhere of their own to keep an
#: etag. One JSON blob in `system_settings` is enough and avoids a table (and a
#: migration) for what is at most a handful of short strings. It is written by
#: this task only — the admin settings form renders a fixed field list, so an
#: extra machine-owned key does not show up there.
FEED_VALIDATORS_KEY = "feed_validators"


@celery_app.task(name="feeds.ingest_all", bind=True, max_retries=2)
def ingest_all(self) -> dict:
    """Fetch every enabled RSS feed once and upsert new Papers (kind="news").

    User-independent — feeds are identical for everyone, so this just fills
    the shared `papers` table. One feed failing (network, bad XML) is logged
    and skipped so the rest still land, same isolation contract as
    `scrape_for_user`.

    Conditional GET: each feed's stored etag/Last-Modified are replayed and a
    304 short-circuits it at zero parse/upsert cost. Validators are read once
    up front and written back in a single `set_system_setting` at the end
    rather than per feed — this runs for the whole deployment, and N commits
    for N feeds would be N times the write for one dict.
    """
    from app.core.settings.service import get_system_setting, set_system_setting
    from app.modules.scrape.models import Paper
    from app.modules.scrape.service import upsert_paper
    from app.modules.scrape.sources import enabled_sources
    from app.modules.scrape.sources.rss_source import FEEDS, fetch_feed_conditional

    try:
        enabled_keys = set(enabled_sources())  # deployment-level SCRAPE_SOURCES filter
        stored = get_system_setting(FEED_VALIDATORS_KEY, {}) or {}
        if not isinstance(stored, dict):  # hand-edited JSON — start clean
            stored = {}
        validators = dict(stored)

        hits = 0
        new = 0
        not_modified = 0
        per_feed: dict[str, int] = {}
        for feed in FEEDS:
            key = feed["key"]
            if key not in enabled_keys:
                continue
            saved = validators.get(key) or {}
            try:
                result = fetch_feed_conditional(
                    feed,
                    etag=saved.get("etag"),
                    last_modified=saved.get("last_modified"),
                )
            except Exception:  # noqa: BLE001 — a flaky feed must not kill the run
                logger.exception("feed_ingest_failed", key=key)
                per_feed[key] = -1
                continue

            if result.status == "not_modified":
                per_feed[key] = 0
                not_modified += 1
                continue
            if result.status != "ok":
                logger.warning("feed_ingest_bad_status", key=key, status=result.status)
                per_feed[key] = -1
                continue

            per_feed[key] = len(result.payloads)
            hits += len(result.payloads)
            for payload in result.payloads:
                existed = (
                    Paper.query.filter_by(
                        source=payload.source, external_id=payload.external_id
                    ).first()
                    is not None
                )
                upsert_paper(payload)
                if not existed:
                    new += 1
            # Recorded after the upserts, so a crash mid-loop leaves the old
            # validator in place and the next run re-fetches in full.
            validators[key] = {"etag": result.etag, "last_modified": result.last_modified}

        if validators != stored:
            set_system_setting(FEED_VALIDATORS_KEY, validators)
        logger.info(
            "feeds_ingest_done", hits=hits, new=new, not_modified=not_modified, feeds=per_feed
        )
        return {"hits": hits, "new": new, "not_modified": not_modified, "feeds": per_feed}
    except Exception as exc:  # noqa: BLE001
        logger.exception("feeds_ingest_all_failed")
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)


@celery_app.task(name="feeds.link_for_user", bind=True, max_retries=2)
def link_for_user(self, user_id: int, *, threshold: int = 60) -> dict:
    """Fetch this user's own custom RSS feeds (Faz 3 Bölüm D — per-user, NOT
    part of `ingest_all`'s global fetch), then score + link the just-ingested
    news backlog (curated feeds + this user's custom feeds) against their
    interests. See `service.link_relevant_feed_items` for the skip conditions
    (every curated feed muted AND no custom feeds, nothing new to score).

    Takes a per-user "feeds" lock. This task fetches N feeds and then makes an
    LLM call, so an overlapping run (beat double-fire, a retry catching up with
    the next schedule) would duplicate both the network work and the spend.
    """
    from app.modules.scrape.service import (
        acquire_user_lock,
        apply_scan_result,
        ingest_user_feeds,
        link_relevant_feed_items,
        record_scan_run,
        release_user_lock,
    )

    user = User.query.filter_by(id=user_id, deleted_at=None).first()
    if user is None:
        logger.warning("feed_link_user_missing", user_id=user_id)
        return {"reason": "user_missing"}

    if not acquire_user_lock(user_id, "feeds"):
        logger.info("feed_link_skip_locked", user_id=user_id)
        return {"reason": "already_running"}

    try:
        with record_scan_run(user_id, "feeds") as run:
            ingest_result, touched = ingest_user_feeds(user)
            res = link_relevant_feed_items(user, threshold=threshold, extra_candidates=touched)
            apply_scan_result(run, {**res, "hits": ingest_result.get("hits", 0)})
        return res
    except Exception as exc:  # noqa: BLE001
        logger.exception("feed_link_failed", user_id=user_id)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)
    finally:
        release_user_lock(user_id, "feeds")


@celery_app.task(name="feeds.link_for_all_users")
def link_for_all_users(*, threshold: int = 60) -> dict:
    """Fan out per-user relevance scoring/linking. Beat calls this once a
    day, after `feeds.ingest_all` and the nightly scrape.

    Spread over SCAN_FANOUT_WINDOW_SECONDS — this fan-out is the one that
    makes an LLM call per user, so bunching it is the most expensive thing we
    could do with it (see app/tasks/fanout.py).
    """
    queued = fan_out(link_for_user, kwargs_for=lambda uid: {"threshold": threshold})
    return {"queued": queued}
