"""Infrastructure liveness for the sidebar status panel.

Deliberately *not* built on `celery inspect ping`. That is a broadcast RPC that
blocks for its full timeout when nothing answers — i.e. it would make every
page render slow precisely when the thing we're trying to detect has happened.

Instead each half stamps a Redis key and we read the keys.

Two keys, because one cannot tell the two halves apart. `core.heartbeat` runs
only when Beat schedules it *and* a worker consumes it, so a fresh stamp proves
both — but a stale one does not say which failed, and the panel used to blame
the worker either way. Stopping Beat with a perfectly healthy worker consuming
tasks reported "Worker: down", which is both wrong and the opposite of useful:
the fix for a stopped scheduler is not restarting the worker.

So the worker stamps its own key from a timer of its own (`app/tasks/
worker_liveness.py`), independent of Beat, and `core.heartbeat` keeps stamping
the other. Worker fresh + heartbeat stale now reads as exactly what it is: the
worker is fine, the scheduler is not.

Still two O(1) lookups, not a broadcast RPC.

Everything here is bounded (0.25s socket timeouts) and swallows its own
errors: a status panel must never be able to break a page render, and "we
can't tell" is a legitimate answer that the UI shows as `unknown`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from flask import current_app

logger = structlog.get_logger()

#: Redis key the beat-scheduled `core.heartbeat` task stamps. Fresh means the
#: scheduler is scheduling *and* a worker is consuming.
HEARTBEAT_KEY = "health:heartbeat"

#: Beat fires `core.heartbeat` every minute; allow a couple of missed beats
#: before calling the scheduler down, so a slow minute isn't a red light.
HEARTBEAT_STALE_AFTER = 180  # seconds

#: Redis key the worker stamps from its own timer, with no Beat involved. This
#: is what separates "the worker died" from "the scheduler died".
WORKER_KEY = "health:worker"

#: The worker stamps every `WORKER_BEAT_INTERVAL` seconds; allow three misses.
WORKER_BEAT_INTERVAL = 30  # seconds
WORKER_STALE_AFTER = 100  # seconds

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


def record_worker_alive(worker: str | None = None) -> None:
    """Stamp the worker's own key. Called by its liveness timer, not by Beat.

    The whole point is that nothing schedules this: a worker that is up but
    idle still says so, which is the case the single-key version got wrong.
    """
    client = _client()
    if client is None:
        return
    try:
        payload = f"{datetime.now(UTC).isoformat()}|{worker or '?'}"
        client.set(WORKER_KEY, payload, ex=WORKER_STALE_AFTER * 2)
    except Exception:  # noqa: BLE001 — a missed stamp is not a failure
        logger.warning("worker_liveness_stamp_failed")


def _read_stamp(client, key: str, stale_after: int) -> tuple[str, datetime | None, str | None]:
    """Read one `<iso>|<name>` stamp and say whether it is fresh.

    Shared by both keys so they cannot drift apart in how they decide "stale".
    """
    try:
        raw = client.get(key)
    except Exception:  # noqa: BLE001
        return UNKNOWN, None, None
    if not raw:
        return DOWN, None, None
    stamp, _, name = raw.partition("|")
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return UNKNOWN, None, None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - seen).total_seconds()
    return (OK if age <= stale_after else DOWN), seen, (name or None)


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
        "beat": UNKNOWN,
        "beat_seen_at": None,
        "queued": None,
        "queues": {},
        "all_ok": False,
    }


def system_health() -> dict:
    """Snapshot of the moving parts a scan depends on.

    Shape::

        {"database": "ok", "redis": "ok",
         "worker": "down", "worker_seen_at": datetime|None, "worker_name": str|None,
         "beat": "ok", "beat_seen_at": datetime|None,
         "queued": 3, "queues": {"celery": 0, "scrape": 3, ...},
         "all_ok": False}

    `worker` and `beat` are independent. A stopped scheduler with a healthy
    worker reads as worker ok / beat down, and the panel says which one to
    restart -- the single-key version blamed the worker for both.

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

    worker, seen_at, worker_name = _read_stamp(client, WORKER_KEY, WORKER_STALE_AFTER)
    beat, beat_seen_at, _ = _read_stamp(client, HEARTBEAT_KEY, HEARTBEAT_STALE_AFTER)

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
        "beat": beat,
        "beat_seen_at": beat_seen_at,
        "queued": sum(queues.values()) if queues else None,
        "queues": queues,
        "all_ok": database == OK and worker == OK and beat == OK,
    }
