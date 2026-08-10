"""Celery integration.

The Celery app is created at import time (so workers can use it via
``celery -A app.tasks``) and wired up to Flask via :func:`init_celery`
during ``create_app()`` so tasks run inside an app context.

Beat schedule lives in ``app/tasks/schedule.py``; concrete tasks live in
``app/tasks/<area>.py``. Workers discover everything by importing
``app.tasks`` — sub-modules are imported at the bottom of this file.
"""

from __future__ import annotations

import structlog
from celery import Celery

logger = structlog.get_logger()

celery_app = Celery("scrapemind")

_flask_app = None

# Queue routing. Three pools with genuinely different shapes:
#   "io"     — feed/HTTP fetching. Network-bound, safe to run wide (threads).
#   "scrape" — academic adapters. Network-bound too, but throttled against
#              shared external quotas, so widening it buys nothing.
#   "llm"    — one paid model call per user per run; deliberately narrow.
# Anything unrouted lands on the default "celery" queue — a worker started with
# an explicit -Q list must therefore always include it, or `core.heartbeat` and
# every future task silently stops running.
TASK_ROUTES = {
    "feeds.ingest_all": {"queue": "io"},
    "scrape.run_for_user": {"queue": "scrape"},
    "scrape.run_for_all_users": {"queue": "scrape"},
    "feeds.link_for_user": {"queue": "llm"},
    "digest.run_for_user": {"queue": "llm"},
    "channels.ingest_for_user": {"queue": "io"},
    "channels.summarize_video": {"queue": "llm"},
    "channels.ingest_for_all_users": {"queue": "io"},
}


def _common_conf(soft_limit: int, hard_limit: int) -> dict:
    """Settings that must be identical whether Celery was configured through
    `create_app()` or through the worker bootstrap below."""
    return {
        "task_routes": TASK_ROUTES,
        "task_soft_time_limit": soft_limit,
        "task_time_limit": hard_limit,
        # Every task here is idempotent (upserts + existence-checked links), so
        # re-delivering one after a worker dies is safe — and much better than
        # losing a night's scan.
        "task_acks_late": True,
        # With acks_late, prefetching would let one slow worker sit on a queue
        # of tasks another worker could have started.
        "worker_prefetch_multiplier": 1,
    }


class LazyContextTask(celery_app.Task):
    """Run task inside Flask app context, initializing the app on demand."""

    def __call__(self, *args, **kwargs):  # type: ignore[override]
        global _flask_app
        if _flask_app is None:
            from app import create_app

            _flask_app = create_app()
        with _flask_app.app_context():
            return self.run(*args, **kwargs)


# Register LazyContextTask as the default task class
celery_app.Task = LazyContextTask


def init_celery(flask_app) -> Celery:
    """Bind the singleton Celery app to a Flask application.

    Reads broker/backend/timezone from Flask config and wraps every task
    in a Flask app-context so handlers can use db, current_app, etc.
    """
    celery_app.conf.update(
        broker_url=flask_app.config["CELERY_BROKER_URL"],
        result_backend=flask_app.config["CELERY_RESULT_BACKEND"],
        task_always_eager=flask_app.config.get("CELERY_TASK_ALWAYS_EAGER", False),
        task_eager_propagates=flask_app.config.get("CELERY_TASK_EAGER_PROPAGATES", True),
        timezone=flask_app.config.get("BABEL_DEFAULT_TIMEZONE", "UTC"),
        enable_utc=True,
        # Periodic schedule lives in app/tasks/schedule.py
        beat_schedule_filename="celerybeat-schedule",  # local file for dev; redbeat in Phase 3
        **_common_conf(
            flask_app.config.get("CELERY_TASK_SOFT_TIME_LIMIT", 600),
            flask_app.config.get("CELERY_TASK_TIME_LIMIT", 900),
        ),
    )

    # Apply the beat schedule lazily (so an empty schedule today doesn't
    # require import-order gymnastics tomorrow).
    from app.tasks.schedule import BEAT_SCHEDULE

    celery_app.conf.beat_schedule = BEAT_SCHEDULE

    class ContextTask(celery_app.Task):
        """Run every task inside the Flask app context."""

        def __call__(self, *args, **kwargs):  # type: ignore[override]
            with flask_app.app_context():
                return self.run(*args, **kwargs)

    celery_app.Task = ContextTask
    return celery_app


# Side-effect: importing this module registers tasks via decorators.
# Keep at the bottom to avoid circular imports.
from app.tasks import (  # noqa: E402, F401
    channel_tasks,
    core_tasks,
    digest_tasks,
    feed_tasks,
    scrape_tasks,
)


def _bootstrap_for_worker() -> None:
    """Read the Flask config class directly to configure Celery without booting the Flask app.

    Inside a Flask request lifecycle this is a no-op: create_app() has
    already called init_celery() with the canonical app object.
    """
    if celery_app.conf.get("broker_url") and celery_app.conf.broker_url.startswith("redis://"):
        return  # already configured
    import os

    if not os.environ.get("CELERY_WORKER_BOOTSTRAP", "1") == "1":
        return
    try:
        from app.config import get_config

        config_cls = get_config()

        _redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        broker_url = os.getenv(
            "CELERY_BROKER_URL", getattr(config_cls, "CELERY_BROKER_URL", _redis_url)
        )
        result_backend = os.getenv(
            "CELERY_RESULT_BACKEND", getattr(config_cls, "CELERY_RESULT_BACKEND", _redis_url)
        )
        always_eager = getattr(config_cls, "CELERY_TASK_ALWAYS_EAGER", False)
        eager_propagates = getattr(config_cls, "CELERY_TASK_EAGER_PROPAGATES", True)
        timezone = getattr(config_cls, "BABEL_DEFAULT_TIMEZONE", "UTC")

        celery_app.conf.update(
            broker_url=broker_url,
            result_backend=result_backend,
            task_always_eager=always_eager,
            task_eager_propagates=eager_propagates,
            timezone=timezone,
            enable_utc=True,
            beat_schedule_filename="celerybeat-schedule",
            **_common_conf(
                int(getattr(config_cls, "CELERY_TASK_SOFT_TIME_LIMIT", 600)),
                int(getattr(config_cls, "CELERY_TASK_TIME_LIMIT", 900)),
            ),
        )

        from app.tasks.schedule import BEAT_SCHEDULE

        celery_app.conf.beat_schedule = BEAT_SCHEDULE
    except Exception:  # noqa: BLE001
        logger.exception("celery_bootstrap_failed")


_bootstrap_for_worker()
