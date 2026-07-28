"""Celery Beat periodic-task schedule.

Empty-friendly: Phase 2 slice 2 ships with one heartbeat to prove Beat
is wired up. Slice 3 (scraping) will populate this with per-source runs.

**All times below are local to `conf.timezone`, which `init_celery` sets from
`BABEL_DEFAULT_TIMEZONE` (Europe/Istanbul), NOT UTC.** `enable_utc=True` only
controls the wire format of message timestamps; crontab matching happens in
`conf.timezone`. Earlier comments here claimed these were UTC hours — they
never were, and `app/tasks/schedule_info.py` resolves next-run times in the
same timezone so what the UI shows matches what Beat does.

`scrape.run_for_all_users` and `feeds.link_for_all_users` do NOT enqueue every
user at the instant they fire: they spread users deterministically across
`SCAN_FANOUT_WINDOW_SECONDS` (see `app/tasks/fanout.py`), so the times below
are the *start* of each window.
"""

from celery.schedules import crontab

BEAT_SCHEDULE = {
    "core-heartbeat-every-minute": {
        "task": "core.heartbeat",
        "schedule": crontab(minute="*"),  # every minute
    },
    # RSS/industry feeds — global, single fetch for the whole deployment
    # (content is identical for every user). Runs before the nightly scrape
    # so freshly-ingested news is available for that same night's relevance
    # linking + digest.
    "feeds-ingest-nightly": {
        "task": "feeds.ingest_all",
        "schedule": crontab(hour=2, minute=45),
    },
    # Nightly fan-out: at 03:15 every day, queue a scrape task for every
    # active user. Each per-user task picks up their keywords + identifiers
    # at that moment.
    "scrape-arxiv-nightly": {
        "task": "scrape.run_for_all_users",
        "schedule": crontab(hour=3, minute=15),
    },
    # Per-user feed relevance scoring + linking — after the nightly scrape so
    # both flows don't contend, before the digest so linked news is included
    # in it.
    "feeds-link-nightly": {
        "task": "feeds.link_for_all_users",
        "schedule": crontab(hour=3, minute=45),
    },
    # Audit retention sweep — after the nightly scrape so the two never
    # contend. AUDIT_RETENTION_DAYS=0 turns the sweep into a no-op.
    "audit-purge-nightly": {
        "task": "core.purge_audit_logs",
        "schedule": crontab(hour=4, minute=0),
    },
    # Drop revoked-token rows whose JWT has expired anyway.
    "revoked-tokens-purge-nightly": {
        "task": "core.purge_revoked_tokens",
        "schedule": crontab(hour=4, minute=15),
    },
    # Scan-history retention sweep — same contract as the audit purge.
    "scan-runs-purge-nightly": {
        "task": "scrape.purge_scan_runs",
        "schedule": crontab(hour=4, minute=30),
    },
    # LLM daily/weekly briefing — after the nightly scrape + purge sweeps so
    # the digest summarises a settled feed. 07:00 local: "ready before the
    # morning coffee", which is what the old (mistakenly UTC-labelled) 04:00
    # entry was aiming at.
    "digest-daily": {
        "task": "digest.run_for_all_users",
        "schedule": crontab(hour=7, minute=0),
        "args": ("daily",),
    },
    # Monday 07:30 local.
    "digest-weekly": {
        "task": "digest.run_for_all_users",
        "schedule": crontab(day_of_week=1, hour=7, minute=30),
        "args": ("weekly",),
    },
}
