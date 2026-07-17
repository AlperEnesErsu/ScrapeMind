"""Redis-backed cache with graceful degradation.

Every function here is a **best-effort optimisation, never a dependency**: if
Redis is unset, unreachable, or throws, callers fall through to the database.
That's deliberate — CI runs without Redis, and a cache outage in production
must slow the app down, not take it down.

Invalidation uses a **version counter** rather than key fan-out. Cache keys
embed the current version (`perms:v3:u42`); bumping the version orphans every
old key at once, and the orphans expire on their own TTL. This matters because
"role X's permissions changed" invalidates every user holding that role —
finding them would mean the query we're trying to avoid.

When Redis is down the version read fails too, so we skip the cache entirely
and read through. No stale data, just no speed-up.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from flask import current_app

logger = structlog.get_logger()

_client: Any = None
_client_ready = False
_warned = False


def _redis():
    """Lazily build the client. Returns None when caching is off/unavailable."""
    global _client, _client_ready, _warned
    if not current_app.config.get("CACHE_ENABLED", False):
        return None
    if _client_ready:
        return _client
    _client_ready = True
    url = current_app.config.get("REDIS_URL") or current_app.config.get("CELERY_BROKER_URL")
    if not url:
        return None
    try:
        import redis  # noqa: PLC0415 — optional at import time

        # Short timeouts: a slow cache must never out-cost the DB read it saves.
        _client = redis.Redis.from_url(
            url, socket_timeout=0.25, socket_connect_timeout=0.25, decode_responses=True
        )
        _client.ping()
        logger.info("cache_connected")
    except Exception as exc:  # noqa: BLE001 — any failure means "no cache", not "no app"
        if not _warned:
            # Once per process, and without a traceback: this path is degraded
            # but healthy, so it shouldn't read like a crash in the logs.
            logger.warning(
                "cache_unavailable_falling_back_to_db", error=str(exc) or type(exc).__name__
            )
            _warned = True
        _client = None
    return _client


def reset_client() -> None:
    """Drop the memoised client. For tests, and after a config change."""
    global _client, _client_ready, _warned
    _client = None
    _client_ready = False
    _warned = False


def get_json(key: str) -> Any | None:
    """Cached value, or None on miss / no cache / any error."""
    client = _redis()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # Corrupt or schema-changed entry — treat as a miss.
        return None


def set_json(key: str, value: Any, ttl: int | None = None) -> None:
    client = _redis()
    if client is None:
        return
    if ttl is None:
        ttl = int(current_app.config.get("CACHE_TTL", 300))
    try:
        client.setex(key, ttl, json.dumps(value))
    except Exception:  # noqa: BLE001
        pass


def get_version(namespace: str) -> int | None:
    """Current invalidation version. None means "no cache available" — the
    caller must then read through rather than guess a version."""
    client = _redis()
    if client is None:
        return None
    try:
        raw = client.get(f"ver:{namespace}")
        return int(raw) if raw is not None else 0
    except Exception:  # noqa: BLE001
        return None


def bump_version(namespace: str) -> None:
    """Invalidate everything in a namespace. A no-op when Redis is absent —
    nothing was cached, so there's nothing to invalidate."""
    client = _redis()
    if client is None:
        return
    try:
        client.incr(f"ver:{namespace}")
        logger.info("cache_invalidated", namespace=namespace)
    except Exception:  # noqa: BLE001
        pass
