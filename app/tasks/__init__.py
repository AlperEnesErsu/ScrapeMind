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
from app.tasks import core_tasks, scrape_tasks  # noqa: E402, F401


def _bootstrap_for_worker() -> None:
    """When the module is loaded by `celery -A app.tasks worker/beat`, Flask's
    create_app() hasn't run yet, so Celery would fall back to the AMQP default
    broker. Build a tiny app instance here just to read config and wire Celery.

    Inside a Flask request lifecycle this is a no-op: create_app() has
    already called init_celery() with the canonical app object.
    """
    if celery_app.conf.get("broker_url") and celery_app.conf.broker_url.startswith("redis://"):
        return  # already configured
    import os

    if not os.environ.get("CELERY_WORKER_BOOTSTRAP", "1") == "1":
        return
    try:
        from app import create_app  # local import — avoids circular at module load

        _flask_app = create_app()
        init_celery(_flask_app)
    except Exception:  # noqa: BLE001
        logger.exception("celery_bootstrap_failed")


_bootstrap_for_worker()
