"""The worker saying "I am here", without asking Beat for permission.

The sidebar used to infer worker liveness from `core.heartbeat`, which Beat
schedules. That stamp proves both halves when it is fresh, but a stale one says
nothing about *which* half stopped -- and the panel blamed the worker. Stopping
Beat on a machine with a perfectly healthy worker reported "Worker: down",
which is wrong and points at the wrong fix.

So the worker stamps a key of its own on a timer it owns. Nothing schedules it,
which is the entire point: a worker that is up but idle still says so.

A thread rather than a periodic task, because a periodic task would need Beat,
which is the thing we are trying to stop depending on. It is a daemon thread,
so it never holds up a shutdown, and every stamp is wrapped -- a liveness
probe that can crash a worker would be a poor trade.
"""

from __future__ import annotations

import threading
import time

import structlog
from celery.signals import worker_ready

logger = structlog.get_logger()

_started = threading.Lock()
_running = False


def _stamp_forever(hostname: str | None) -> None:
    from app.core.health import WORKER_BEAT_INTERVAL, record_worker_alive

    while True:
        try:
            # The same lazily built Flask app the tasks use -- `record_worker_alive`
            # reads the broker URL off `current_app.config`.
            from app.tasks import flask_app_for_worker

            with flask_app_for_worker().app_context():
                record_worker_alive(hostname)
        except Exception:  # noqa: BLE001 — never let liveness kill the worker
            logger.warning("worker_liveness_tick_failed", exc_info=True)
        time.sleep(WORKER_BEAT_INTERVAL)


@worker_ready.connect
def start_liveness_timer(sender=None, **_kwargs) -> None:
    """Begin stamping as soon as the worker is ready to consume."""
    global _running

    with _started:
        if _running:
            return
        _running = True

    hostname = getattr(sender, "hostname", None)
    thread = threading.Thread(
        target=_stamp_forever,
        args=(hostname,),
        name="worker-liveness",
        daemon=True,
    )
    thread.start()
    logger.info("worker_liveness_started", worker=hostname)
