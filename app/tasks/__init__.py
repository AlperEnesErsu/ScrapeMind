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
#   "io"     — feed/HTTP fetching (and other network-light housekeeping).
#              Network-bound or cheap, safe to run wide (threads).
#   "scrape" — academic adapters. Network-bound too, but throttled against
#              shared external quotas, so widening it buys nothing.
#   "llm"    — one paid model call per user per run; deliberately narrow.
# Anything left off this table lands on the default "celery" queue — a worker
# started with an explicit -Q list must therefore always include it, or every
# task missing from here silently stops running. Queue/worker -Q mismatches
# are a bug class that has already bitten this deployment once in prod (see
# `docs/HANDOVER.md` §3 commit `a108514`: the worker ran with no -Q at all, so
# it only drained the default queue and every *routed* task silently never
# ran until that was fixed — the mirror image of the risk this table guards
# against now). `INTENTIONALLY_UNROUTED_TASKS` below is the only accepted
# exception to "every task needs a route" — every other task, and especially
# anything Beat fans out, must be listed here (see the regression check in
# tests/modules/test_celery_smoke.py).
TASK_ROUTES = {
    "feeds.ingest_all": {"queue": "io"},
    "scrape.run_for_user": {"queue": "scrape"},
    "scrape.run_for_all_users": {"queue": "scrape"},
    "feeds.link_for_user": {"queue": "llm"},
    # Fan-out parent for `feeds.link_for_user`. It doesn't call the LLM
    # itself, but every fan-out parent in this table shares its child's
    # queue (see `scrape.run_for_all_users`, `channels.ingest_for_all_users`,
    # `authors.ingest_for_all_users` below) so one pool's worker count models
    # "capacity for this kind of work" as a unit. This was one of the two
    # fan-out parents found unrouted (silently landing on the default
    # "celery" queue) — see `digest.run_for_all_users` below for the other.
    "feeds.link_for_all_users": {"queue": "llm"},
    "digest.run_for_user": {"queue": "llm"},
    "digest.run_for_all_users": {"queue": "llm"},
    "channels.ingest_for_user": {"queue": "io"},
    "channels.summarize_video": {"queue": "llm"},
    "channels.ingest_for_all_users": {"queue": "io"},
    "patents.ingest_for_user": {"queue": "io"},
    "patents.ingest_for_all_users": {"queue": "io"},
    # `scrape`, not `io`: these hit the same OpenAlex budget as the academic
    # scan, so they belong in the same pool rather than racing it for tokens.
    "authors.ingest_for_user": {"queue": "scrape"},
    "authors.ingest_for_all_users": {"queue": "scrape"},
    # Nightly retention sweeps: local DB deletes only — no external network
    # call, no shared quota, no paid model call — so neither `scrape` nor
    # `llm`'s throttling rationale applies. `io` is the general-purpose wide
    # pool, and that's exactly the shape this work has.
    "core.purge_audit_logs": {"queue": "io"},
    "core.purge_revoked_tokens": {"queue": "io"},
    "scrape.purge_scan_runs": {"queue": "io"},
    # On-demand only (no fan-out parent, no BEAT_SCHEDULE entry): one paid
    # LLM map/reduce batch per run, same pool as the digest/link-for-user
    # LLM work for the same "capacity for this kind of work" reason.
    "reports.generate": {"queue": "llm"},
    # Embedding generation is a paid provider call per paper, so it shares the
    # `llm` pool's capacity rather than flooding `io` with billable work.
    "embeddings.embed_paper": {"queue": "llm"},
    "embeddings.embed_pending_papers": {"queue": "llm"},
    # Full text is a network fetch plus a PDF parse -- I/O bound and not
    # billable, so it belongs with the other fetchers rather than in `llm`.
    # What it feeds (analysis, embeddings) is billable and is queued
    # separately by those tasks.
    "fulltext.fetch_for_paper": {"queue": "io"},
    "fulltext.fetch_pending": {"queue": "io"},
    # Saved-search alerts are a database query and a notification -- no LLM
    # call anywhere in the path, so they do not belong in `llm`.
    "alerts.run_for_user": {"queue": "io"},
    "alerts.run_for_all_users": {"queue": "io"},
}

# Tasks deliberately left off TASK_ROUTES, so they land on the default
# "celery" queue on purpose rather than by oversight. Each entry here must be
# safe to silently stop running if a deployment's workers are started with a
# -Q list that omits "celery":
#   - core.heartbeat: a liveness probe, not a job with a required outcome.
#     Beat fires it every minute; if nothing consumes the default queue the
#     only symptom is a stale worker-health stamp (app/core/health.py) on the
#     admin panel — never lost or delayed user-facing work. Routing it into
#     "io"/"scrape"/"llm" would also defeat its purpose: it exists to prove
#     *some* worker in the deployment is alive, not one specialized pool.
INTENTIONALLY_UNROUTED_TASKS = frozenset({"core.heartbeat"})


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


def flask_app_for_worker():
    """The one Flask app a worker process builds, built on first use.

    `LazyContextTask` owns this global; this is the same app, exposed so the
    liveness timer can push a context without creating a second application
    with its own connection pools.
    """
    global _flask_app

    if _flask_app is None:
        from app import create_app

        _flask_app = create_app()
    return _flask_app


class LazyContextTask(celery_app.Task):
    """Run task inside Flask app context, initializing the app on demand.

    An app context that is *already* pushed wins over the lazily built one.
    In a worker there is never one, so this changes nothing there — it is
    about eager execution (`CELERY_TASK_ALWAYS_EAGER`), where the caller
    already has an app: a web request in dev, or a test's `app` fixture.
    Building a second `create_app()` underneath those would run the task
    against a different Flask app than the caller configured, so config set
    by the caller — `monkeypatch.setitem(app.config, ...)` in a test, most
    visibly — would silently not apply. That mismatch was real and
    order-dependent: whether a given eager task saw the caller's config or a
    private second app depended on which test had run first.
    """

    def __call__(self, *args, **kwargs):  # type: ignore[override]
        global _flask_app
        from flask import has_app_context

        if has_app_context():
            return self.run(*args, **kwargs)
        with flask_app_for_worker().app_context():
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
        """Run every task inside the Flask app context.

        Note this rebinds the process-global `celery_app.Task` to a class
        closed over *this* `flask_app`, so in a process that builds more than
        one app the last `create_app()` owns every task's context. Production
        has exactly one app and never notices; a test session has several
        (the session fixture, plus any test that builds its own), which made
        "which app does an eager task see?" depend on collection order.

        Hence the same rule as `LazyContextTask`: an app context that is
        already pushed wins. A worker never has one, so its behaviour is
        unchanged — but an eager caller now always gets its own app, and its
        own config, instead of whichever app happened to be created last.
        """

        def __call__(self, *args, **kwargs):  # type: ignore[override]
            from flask import has_app_context

            if has_app_context():
                return self.run(*args, **kwargs)
            with flask_app.app_context():
                return self.run(*args, **kwargs)

    celery_app.Task = ContextTask
    return celery_app


# Side-effect: importing this module registers tasks via decorators.
# Keep at the bottom to avoid circular imports.
from app.tasks import (  # noqa: E402, F401
    alert_tasks,
    author_tasks,
    channel_tasks,
    core_tasks,
    digest_tasks,
    embedding_tasks,
    feed_tasks,
    fulltext_tasks,
    patent_tasks,
    report_tasks,
    scrape_tasks,
    # Not a task module. Importing it connects the `worker_ready` signal that
    # starts the worker's own liveness timer -- without this line the worker
    # never stamps its key and the sidebar reports it down.
    worker_liveness,
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
