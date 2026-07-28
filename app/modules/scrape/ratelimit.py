"""Deployment-wide token buckets for shared external APIs.

Each source adapter already throttles itself — `arxiv.Client(delay_seconds=3)`,
Semantic Scholar's documented ~100 requests / 5 minutes — but those limits are
per *process*. The scrape architecture is per *user*, so the moment two workers
run two users' scans concurrently, every one of those limits is multiplied by
the worker count and the shared quota is the thing that breaks first.

This module puts the limit back where the quota actually lives: one Redis
counter per (bucket, window), shared by every worker.

Fail-open by design, same contract as the scrape lock in `service.py`: if Redis
is unreachable we let the request through rather than stalling every scan. A
missing rate limiter degrades to today's behaviour, not to an outage.
"""

from __future__ import annotations

import time

import structlog
from flask import current_app

logger = structlog.get_logger()

# How long we're willing to wait for a slot before giving up and proceeding.
# Longer than this and the task's own soft time limit becomes the real cap.
_DEFAULT_MAX_WAIT = 30.0
_POLL_INTERVAL = 0.5


class SourceThrottledError(RuntimeError):
    """A source was skipped because of a rate limit, not because it had
    nothing to say.

    The distinction matters all the way up to the UI: a source that returns
    zero payloads is recorded as a successful scan with no new papers, and the
    dashboard tells the user "0 new" — which reads as "there is nothing out
    there". Raising instead lets `service.scrape_for_user` mark the source with
    its `-1` sentinel, which turns the run into `status="partial"` and puts the
    warning icon on the scan-status line. Same content, honest label.
    """


def _client():
    url = current_app.config.get("CELERY_BROKER_URL") or current_app.config.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis  # noqa: PLC0415 — optional dependency

        client = redis.Redis.from_url(
            url, socket_timeout=0.25, socket_connect_timeout=0.25, decode_responses=True
        )
        client.ping()
        return client
    except Exception:  # noqa: BLE001 — no redis means "no limit", not "no scrape"
        return None


def _try_consume(client, bucket: str, limit: int, per_seconds: int) -> bool:
    """Fixed-window counter: INCR a key that expires with the window.

    Fixed windows can allow up to 2x the limit across a boundary; that is
    acceptable here because the published quotas already have headroom and the
    alternative (a sliding log) costs far more Redis work per request.
    """
    window = int(time.time() // per_seconds)
    key = f"ratelimit:{bucket}:{window}"
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, per_seconds + 1)
        count, _ = pipe.execute()
        return int(count) <= limit
    except Exception:  # noqa: BLE001
        return True  # fail-open


def acquire_slot(
    bucket: str,
    limit: int,
    per_seconds: int,
    *,
    max_wait: float = _DEFAULT_MAX_WAIT,
) -> bool:
    """Block until this deployment may make one more `bucket` request.

    Returns True when a slot was obtained (or when the limiter is unavailable
    and we're failing open), False when `max_wait` elapsed first — in which
    case the caller should skip the request rather than blow the quota.
    """
    if limit <= 0:
        return True
    client = _client()
    if client is None:
        return True

    deadline = time.monotonic() + max_wait
    while True:
        if _try_consume(client, bucket, limit, per_seconds):
            return True
        if time.monotonic() >= deadline:
            logger.warning("ratelimit_gave_up", bucket=bucket, limit=limit, per=per_seconds)
            return False
        time.sleep(_POLL_INTERVAL)


def arxiv_slot() -> bool:
    return acquire_slot("arxiv", int(_cfg("SCRAPE_RATE_ARXIV_PER_MIN", 20)), 60)


def semantic_scholar_slot() -> bool:
    return acquire_slot("s2", int(_cfg("SCRAPE_RATE_S2_PER_5MIN", 100)), 300)


def pubmed_slot() -> bool:
    return acquire_slot("pubmed", int(_cfg("SCRAPE_RATE_PUBMED_PER_SEC", 3)), 1)


def _cfg(key: str, default):
    try:
        return current_app.config.get(key, default)
    except Exception:  # noqa: BLE001 — outside an app context
        return default
