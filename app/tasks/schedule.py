"""Celery Beat periodic-task schedule.

Empty-friendly: Phase 2 slice 2 ships with one heartbeat to prove Beat
is wired up. Slice 3 (scraping) will populate this with per-source runs.
"""

from celery.schedules import crontab

BEAT_SCHEDULE = {
    "core-heartbeat-every-minute": {
        "task": "core.heartbeat",
        "schedule": crontab(minute="*"),  # every minute
    },
    # Nightly fan-out: at 03:15 every day, queue a scrape task for every
    # active user. Each per-user task picks up their keywords + identifiers
    # at that moment.
    "scrape-arxiv-nightly": {
        "task": "scrape.run_for_all_users",
        "schedule": crontab(hour=3, minute=15),
    },
    # Audit retention sweep — after the nightly scrape so the two never
    # contend. AUDIT_RETENTION_DAYS=0 turns the sweep into a no-op.
    "audit-purge-nightly": {
        "task": "core.purge_audit_logs",
        "schedule": crontab(hour=4, minute=0),
    },
}
