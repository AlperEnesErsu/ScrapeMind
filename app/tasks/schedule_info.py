"""Resolve the beat schedule into concrete "next run" timestamps.

The dashboard used to state "updates automatically every night at 03:15" as a
translated string literal. That is a claim about `app/tasks/schedule.py` that
nothing verified, so it would go quietly wrong the first time anyone moved the
schedule. Everything user-facing about scan timing now derives from here.

Crontab entries are matched in `celery_app.conf.timezone`, which `init_celery`
sets from `BABEL_DEFAULT_TIMEZONE` (Europe/Istanbul) — *not* UTC, despite
`enable_utc=True`, which only affects message timestamps. `remaining_estimate`
handles that for us; we return an aware UTC datetime and let Babel render it in
the viewer's locale.

Every function here degrades to `None` rather than raising: a deployment
running without beat configured should show "runs automatically" instead of a
500.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from app.tasks import celery_app
from app.tasks.fanout import user_fanout_offset

logger = structlog.get_logger()


def _app_tz():
    """The timezone Beat matches crontabs in (conf.timezone), falling back to
    UTC if Celery hasn't been configured yet."""
    try:
        return celery_app.timezone or UTC
    except Exception:  # noqa: BLE001
        return UTC


def _entries_for(task_name: str) -> list:
    schedule = getattr(celery_app.conf, "beat_schedule", None) or {}
    return [e for e in schedule.values() if isinstance(e, dict) and e.get("task") == task_name]


def next_run_at(task_name: str, *, now: datetime | None = None) -> datetime | None:
    """Earliest upcoming fire time for `task_name`, as aware UTC.

    A task can legitimately have several schedule entries (the digest has a
    daily and a weekly one), so we take the soonest.
    """
    now = now or datetime.now(UTC)
    best: datetime | None = None
    for entry in _entries_for(task_name):
        sched = entry.get("schedule")
        if sched is None or not hasattr(sched, "remaining_estimate"):
            continue
        try:
            # crontab resolves `app` lazily via current_app; bind it explicitly
            # so this works in a worker that never went through create_app().
            sched.app = celery_app
            # `remaining_estimate` does its hour/minute arithmetic on whatever
            # datetime it is handed: pass an aware-UTC value and a
            # `crontab(hour=3)` resolves to 03:00 UTC, which is NOT when Beat
            # fires it. Converting to the app timezone first is what makes the
            # result match reality (Beat matches crontabs in conf.timezone).
            remaining = sched.remaining_estimate(now.astimezone(_app_tz()))
        except Exception:  # noqa: BLE001 — a broken entry must not break the page
            logger.warning("next_run_estimate_failed", task=task_name)
            continue
        if remaining is None:
            continue
        if remaining < timedelta(0):
            remaining = timedelta(0)
        # Snap to the nearest minute. Crontabs always fire at HH:MM:00, but
        # `remaining_estimate` measures the delta against Celery's *own* clock
        # reading, so `now + remaining` lands a fraction of a second either
        # side of the true instant. Flooring would then disagree by a whole
        # minute whenever that fraction straddles a second boundary; rounding
        # absorbs it.
        candidate = (now + remaining + timedelta(seconds=30)).replace(second=0, microsecond=0)
        if best is None or candidate < best:
            best = candidate
    return best


def next_scan_at_for_user(user, *, now: datetime | None = None) -> datetime | None:
    """When *this* user's papers get scanned next.

    The nightly fan-out spreads users deterministically over
    `SCAN_FANOUT_WINDOW_SECONDS`, so a user's real scan time is the schedule's
    fire time plus their own stable offset — that offset is what makes the
    displayed time accurate instead of merely plausible.
    """
    base = next_run_at("scrape.run_for_all_users", now=now)
    if base is None:
        return None
    return base + timedelta(seconds=user_fanout_offset(user.id))


def schedule_overview() -> list[dict]:
    """Every beat entry with its resolved next fire time — for /admin/tasks."""
    now = datetime.now(UTC)
    schedule = getattr(celery_app.conf, "beat_schedule", None) or {}
    out: list[dict] = []
    for name, entry in schedule.items():
        if not isinstance(entry, dict):
            continue
        task = entry.get("task", "")
        out.append(
            {
                "name": name,
                "task": task,
                "schedule": str(entry.get("schedule", "")),
                "args": entry.get("args", ()),
                "queue": (celery_app.conf.task_routes or {}).get(task, {}).get("queue", "celery"),
                "next_run_at": next_run_at(task, now=now),
            }
        )
    out.sort(key=lambda e: (e["next_run_at"] is None, e["next_run_at"] or now))
    return out
