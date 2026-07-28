"""Infrastructure liveness for the sidebar status panel.

Deliberately *not* built on `celery inspect ping`. That is a broadcast RPC that
blocks for its full timeout when nothing answers — i.e. it would make every
page render slow precisely when the thing we're trying to detect has happened.

Instead we lean on the `core.heartbeat` task that Beat already runs every
minute: it stamps a Redis key, and we read that one key. A fresh stamp proves
*both* that Beat is scheduling and that a worker is consuming — a strictly
better signal than an inspect ping, for one O(1) lookup.

Everything here is bounded (0.25s socket timeouts) and swallows its own
errors: a status panel must never be able to break a page render, and "we
can't tell" is a legitimate answer that the UI shows as `unknown`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from flask import current_app

logger = structlog.get_logger()

#: Redis key the heartbeat task stamps. Read by `system_health`.
HEARTBEAT_KEY = "health:heartbeat"

#: Beat fires `core.heartbeat` every minute; allow a couple of missed beats
#: before calling the worker down, so a slow minute isn't a red light.
HEARTBEAT_STALE_AFTER = 180  # seconds

#: Queues declared in `app/tasks/__init__.py:TASK_ROUTES`, plus the default.
_QUEUES = ("celery", "io", "scrape", "llm")

OK = "ok"
DOWN = "down"
UNKNOWN = "unknown"


def _client():
    """Best-effort Redis client on the Celery broker, or None."""
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
    except Exception:  # noqa: BLE001
        return None


def record_heartbeat(worker: str | None = None) -> None:
    """Stamp the heartbeat key. Called by `core.heartbeat` on every beat.

    The key carries a TTL a little longer than the staleness window so a
    stopped worker's stamp disappears on its own rather than lingering as a
    misleading "last seen".
    """
    client = _client()
    if client is None:
        return
    try:
        payload = f"{datetime.now(UTC).isoformat()}|{worker or '?'}"
        client.set(HEARTBEAT_KEY, payload, ex=HEARTBEAT_STALE_AFTER * 2)
    except Exception:  # noqa: BLE001 — a missed stamp is not a task failure
        logger.warning("heartbeat_stamp_failed")


def _read_heartbeat(client) -> tuple[str, datetime | None, str | None]:
    try:
        raw = client.get(HEARTBEAT_KEY)
    except Exception:  # noqa: BLE001
        return UNKNOWN, None, None
    if not raw:
        return DOWN, None, None
    stamp, _, worker = raw.partition("|")
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return UNKNOWN, None, None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - seen).total_seconds()
    return (OK if age <= HEARTBEAT_STALE_AFTER else DOWN), seen, (worker or None)


def _database_status() -> str:
    from sqlalchemy import text

    from app.extensions import db

    try:
        db.session.execute(text("SELECT 1"))
        return OK
    except Exception:  # noqa: BLE001
        logger.warning("health_db_check_failed")
        return DOWN


def _unknown(database: str = UNKNOWN, redis: str = DOWN) -> dict:
    return {
        "database": database,
        "redis": redis,
        "worker": UNKNOWN,
        "worker_seen_at": None,
        "worker_name": None,
        "queued": None,
        "queues": {},
        "all_ok": False,
    }


def system_health() -> dict:
    """Snapshot of the moving parts a scan depends on.

    Shape::

        {"database": "ok", "redis": "ok", "worker": "down",
         "worker_seen_at": datetime|None, "worker_name": str|None,
         "queued": 3, "queues": {"celery": 0, "scrape": 3, ...},
         "all_ok": False}

    `queued` is the total depth across the declared queues — with a healthy
    worker it is ~0, and a number that only grows is the visible symptom of
    "tasks are being produced faster than they're consumed".

    **Total by construction**: every failure path resolves to `unknown` rather
    than an exception. This renders inside the sidebar on every admin page, and
    a status widget that can 500 the app is worse than no status widget.
    """
    try:
        return _probe()
    except Exception:  # noqa: BLE001 — see the docstring: never break a render
        logger.exception("health_probe_failed")
        return _unknown()


def _probe() -> dict:
    client = _client()
    if client is None:
        return _unknown(database=_database_status())

    worker, seen_at, worker_name = _read_heartbeat(client)

    queues: dict[str, int] = {}
    try:
        for q in _QUEUES:
            queues[q] = int(client.llen(q) or 0)
    except Exception:  # noqa: BLE001 — depth is nice-to-have, not load-bearing
        queues = {}

    database = _database_status()
    return {
        "database": database,
        "redis": OK,
        "worker": worker,
        "worker_seen_at": seen_at,
        "worker_name": worker_name,
        "queued": sum(queues.values()) if queues else None,
        "queues": queues,
        "all_ok": database == OK and worker == OK,
    }
