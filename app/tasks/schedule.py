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
}
