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

The bottom half of this module (`consume_quota` and friends, Faz 5.1) is the
deliberate exception: cumulative weekly budgets for licensed sources live in
Postgres and fail **closed**. Overrunning a contracted quota because a cache
was down is worse than skipping a night's scan. Both are called together —
`<name>_slot()` for the instantaneous rate, `consume_quota()` for the budget.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

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
    url = _cfg("CELERY_BROKER_URL", None) or _cfg("REDIS_URL", None)
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


def openalex_slot() -> bool:
    return acquire_slot("openalex", int(_cfg("SCRAPE_RATE_OPENALEX_PER_SEC", 8)), 1)


def crossref_slot() -> bool:
    return acquire_slot("crossref", int(_cfg("SCRAPE_RATE_CROSSREF_PER_SEC", 5)), 1)


def web_reach_slot() -> bool:
    return acquire_slot("web_reach", int(_cfg("SCRAPE_RATE_WEB_PER_MIN", 30)), 60)


def youtube_reach_slot() -> bool:
    return acquire_slot("youtube_reach", int(_cfg("SCRAPE_RATE_YOUTUBE_PER_MIN", 30)), 60)


def github_reach_slot() -> bool:
    return acquire_slot("github_reach", int(_cfg("SCRAPE_RATE_GITHUB_PER_MIN", 30)), 60)


def youtube_channel_slot() -> bool:
    return acquire_slot("youtube_channel", int(_cfg("SCRAPE_RATE_YT_CHANNEL_PER_MIN", 30)), 60)


def epo_ops_slot() -> bool:
    """EPO publishes no per-second figure, only the weekly 4 GB tier and a
    "fair use" clause. 10/min is a conservative shape for a nightly batch —
    the real ceiling is the byte budget in `consume_quota`."""
    return acquire_slot("epo_ops", int(_cfg("SCRAPE_RATE_EPO_OPS_PER_MIN", 10)), 60)


def patentsview_slot() -> bool:
    """PatentsView documents 45 requests/minute."""
    return acquire_slot("patentsview", int(_cfg("SCRAPE_RATE_PATENTSVIEW_PER_MIN", 45)), 60)


def scopus_slot() -> bool:
    """Elsevier documents 9 requests/second for Scopus Search. The weekly
    20.000 budget in `consume_quota` is the binding limit."""
    return acquire_slot("scopus", int(_cfg("SCRAPE_RATE_SCOPUS_PER_SEC", 9)), 1)


def bluesky_slot() -> bool:
    """Bluesky public XRPC AppView slot — default 60 requests/minute."""
    return acquire_slot("bluesky", int(_cfg("SCRAPE_RATE_BLUESKY_PER_MIN", 60)), 60)


# ----------------------------------------------------------------------------
# Cumulative weekly quotas — Postgres, fail-closed (Faz 5.1)
# ----------------------------------------------------------------------------
#
# Everything above is a per-second/per-minute *rate* limiter in Redis and fails
# open. Everything below is a per-week *budget* in Postgres and fails closed.
# See `SourceQuotaUsage`'s docstring for why the two cannot be the same thing.


def quota_window_start(now: datetime | None = None) -> datetime:
    """Start of the quota week containing `now`: Monday 00:00 UTC.

    UTC, not `BABEL_DEFAULT_TIMEZONE`. The published quotas belong to the
    upstream provider, and the reset boundary has to be the same instant for
    every worker regardless of where it runs — this is the one place in the
    codebase where the local timezone would be actively wrong.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


def quota_limit(name: str) -> int:
    """Weekly request budget for `name` from SCRAPE_QUOTA_<NAME>_WEEKLY.

    0 (the default) means unmetered — every source through Faz 4 — and
    `consume_quota` lets those through untouched rather than writing rows
    nothing will ever read.
    """
    return int(_cfg(f"SCRAPE_QUOTA_{name.upper()}_WEEKLY", 0) or 0)


def quota_byte_limit(name: str) -> int:
    """Weekly byte budget for `name` from SCRAPE_QUOTA_<NAME>_WEEKLY_BYTES.
    0 means the source meters calls, not bandwidth."""
    return int(_cfg(f"SCRAPE_QUOTA_{name.upper()}_WEEKLY_BYTES", 0) or 0)


def consume_quota(name: str, *, cost: int = 1, bytes_: int = 0) -> bool:
    """Spend `cost` requests (and `bytes_` bytes) from `name`'s weekly budget.

    Returns True when the spend fit inside the budget and was recorded, False
    when it would exceed it — in which case the caller must raise
    `SourceThrottledError` rather than return an empty list, so the run is
    labelled "partial" instead of lying about having found nothing.

    Atomic by construction: the spend and the limit check are one
    `UPDATE ... WHERE used + cost <= limit RETURNING id` statement, so two
    workers racing on the last slot cannot both win. The row is created on
    first use of each week; a lost INSERT race is caught and retried as an
    UPDATE against the row the other worker just committed.

    **Fail-closed.** Any database error returns False. An unmetered source
    (limit 0 and byte limit 0) returns True without touching the database.
    """
    limit = quota_limit(name)
    byte_limit = quota_byte_limit(name)
    if limit <= 0 and byte_limit <= 0:
        return True

    from sqlalchemy import text

    from app.extensions import db

    window = quota_window_start()
    # An unset budget on one axis must not block the other: a source metered
    # only on bytes has limit 0, and `used + cost <= 0` would reject every
    # call. Treat 0 as "no ceiling on this axis" by comparing against a bound
    # the counter cannot reach.
    req_ceiling = limit if limit > 0 else None
    byte_ceiling = byte_limit if byte_limit > 0 else None

    conditions = []
    params = {
        "name": name,
        "window": window,
        "cost": int(cost),
        "bytes": int(bytes_),
    }
    if req_ceiling is not None:
        conditions.append("requests_used + :cost <= :req_ceiling")
        params["req_ceiling"] = req_ceiling
    if byte_ceiling is not None:
        conditions.append("bytes_used + :bytes <= :byte_ceiling")
        params["byte_ceiling"] = byte_ceiling
    where_budget = " AND ".join(conditions)

    try:
        for _attempt in range(2):
            row = db.session.execute(
                text(f"""
                    UPDATE source_quota_usage
                       SET requests_used = requests_used + :cost,
                           bytes_used    = bytes_used + :bytes,
                           updated_at    = now()
                     WHERE source_name = :name
                       AND window_start = :window
                       AND {where_budget}
                 RETURNING id
                    """),  # noqa: S608 — `where_budget` is built from literals above
                params,
            ).first()
            if row is not None:
                db.session.commit()
                return True

            # No row updated: either this week's row does not exist yet, or the
            # budget is genuinely spent. Only the first case is retryable.
            exists = db.session.execute(
                text(
                    "SELECT 1 FROM source_quota_usage "
                    "WHERE source_name = :name AND window_start = :window"
                ),
                {"name": name, "window": window},
            ).first()
            if exists is not None:
                db.session.rollback()
                logger.warning("source_quota_exhausted", source=name, window=window.isoformat())
                return False

            try:
                db.session.execute(
                    text(
                        "INSERT INTO source_quota_usage "
                        "(source_name, window_start, requests_used, bytes_used, "
                        " created_at, updated_at) "
                        "VALUES (:name, :window, 0, 0, now(), now()) "
                        "ON CONFLICT (source_name, window_start) DO NOTHING"
                    ),
                    {"name": name, "window": window},
                )
                db.session.commit()
            except Exception:  # noqa: BLE001 — another worker inserted it first
                db.session.rollback()
        return False
    except Exception:  # noqa: BLE001 — see the docstring: fail closed
        logger.exception("source_quota_check_failed", source=name)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def quota_usage(name: str) -> dict:
    """This week's consumption for `name`, for the admin health panel.

    Shape: ``{"source": str, "window_start": datetime, "requests_used": int,
    "requests_limit": int, "bytes_used": int, "bytes_limit": int}``. Returns
    zeroed counters (never raises) when nothing has been spent yet or the
    lookup fails — a status panel must not be able to break a render.
    """
    window = quota_window_start()
    out = {
        "source": name,
        "window_start": window,
        "requests_used": 0,
        "requests_limit": quota_limit(name),
        "bytes_used": 0,
        "bytes_limit": quota_byte_limit(name),
    }
    try:
        from app.modules.scrape.models import SourceQuotaUsage

        row = SourceQuotaUsage.query.filter_by(source_name=name, window_start=window).first()
    except Exception:  # noqa: BLE001
        logger.warning("source_quota_read_failed", source=name)
        return out
    if row is not None:
        out["requests_used"] = int(row.requests_used or 0)
        out["bytes_used"] = int(row.bytes_used or 0)
    return out


def _cfg(key: str, default):
    try:
        return current_app.config.get(key, default)
    except Exception:  # noqa: BLE001 — outside an app context
        return default
