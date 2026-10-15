"""Per-user Redis scan locks."""

from __future__ import annotations

import structlog
from flask import current_app

logger = structlog.get_logger()


# Per-user run locks — collapse duplicate presses / overlapping nightly runs

SCRAPE_LOCK_TTL = 900  # seconds — safety expiry if a worker dies mid-run


def _lock_client():
    """Best-effort redis client on the Celery broker. Returns None (→ fail-open)
    when redis is unset/unreachable, so locking never blocks scraping."""
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
    except Exception:  # noqa: BLE001 — no redis means "no lock", not "no scrape"
        return None


def _lock_key(user_id: int, kind: str) -> str:
    return f"scan:lock:{kind}:{user_id}"


def acquire_user_lock(user_id: int, kind: str = "scrape", *, ttl: int | None = None) -> bool:
    """Try to claim this user's slot for `kind` ("scrape" | "feeds").

    Returns True if claimed (caller may proceed), False if a run of the same
    kind is already queued/running for this user. Fail-open: if redis is
    unavailable we return True — never block work over a missing lock.

    The default TTL follows the hard task time limit so the lock can't outlive
    a worker that was killed mid-run.
    """
    client = _lock_client()
    if client is None:
        return True
    if ttl is None:
        ttl = int(current_app.config.get("CELERY_TASK_TIME_LIMIT", SCRAPE_LOCK_TTL))
    try:
        return bool(client.set(_lock_key(user_id, kind), "1", nx=True, ex=ttl))
    except Exception:  # noqa: BLE001
        return True


def release_user_lock(user_id: int, kind: str = "scrape") -> None:
    """Free this user's slot for `kind` (called by the task when it finishes)."""
    client = _lock_client()
    if client is None:
        return
    try:
        client.delete(_lock_key(user_id, kind))
    except Exception:  # noqa: BLE001
        pass


# Note: there is deliberately no "is this user locked?" helper for the UI.
# The lock means "queued or running" and is claimed by the HTTP route before a
# worker touches the task, so a deployment with no worker would hold it for the
# full TTL and the dashboard would claim to be scanning the whole time. The UI
# reads `open_scan_run` instead — a row only exists once a task really started.


# Kept so the manual-scrape route and its tests keep working unchanged.
def acquire_scrape_lock(user_id: int) -> bool:
    return acquire_user_lock(user_id, "scrape")


def release_scrape_lock(user_id: int) -> None:
    release_user_lock(user_id, "scrape")
