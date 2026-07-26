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


@celery_app.task(name="feeds.ingest_all", bind=True, max_retries=2)
def ingest_all(self) -> dict:
    """Fetch every enabled RSS feed once and upsert new Papers (kind="news").

    User-independent — feeds are identical for everyone, so this just fills
    the shared `papers` table. One feed failing (network, bad XML) is logged
    and skipped so the rest still land, same isolation contract as
    `scrape_for_user`.
    """
    from app.modules.scrape.models import Paper
    from app.modules.scrape.service import upsert_paper
    from app.modules.scrape.sources import enabled_sources
    from app.modules.scrape.sources.rss_source import FEEDS, fetch_feed

    try:
        enabled_keys = set(enabled_sources())  # deployment-level SCRAPE_SOURCES filter
        hits = 0
        new = 0
        per_feed: dict[str, int] = {}
        for feed in FEEDS:
            key = feed["key"]
            if key not in enabled_keys:
                continue
            try:
                payloads = fetch_feed(feed)
            except Exception:  # noqa: BLE001 — a flaky feed must not kill the run
                logger.exception("feed_ingest_failed", key=key)
                per_feed[key] = -1
                continue
            per_feed[key] = len(payloads)
            hits += len(payloads)
            for payload in payloads:
                existed = (
                    Paper.query.filter_by(
                        source=payload.source, external_id=payload.external_id
                    ).first()
                    is not None
                )
                upsert_paper(payload)
                if not existed:
                    new += 1
        logger.info("feeds_ingest_done", hits=hits, new=new, feeds=per_feed)
        return {"hits": hits, "new": new, "feeds": per_feed}
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
