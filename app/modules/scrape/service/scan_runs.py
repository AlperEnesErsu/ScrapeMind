"""`ScanRun` lifecycle, cost estimates and scan status."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import structlog
from flask import current_app
from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db
from app.modules.academic.service import list_user_keywords
from app.modules.scrape.models import (
    ScanRun,
)
from app.modules.scrape.service.feeds import list_user_feeds
from app.modules.scrape.service.locks import SCRAPE_LOCK_TTL
from app.modules.scrape.service.scraping import _PER_KEYWORD_REQUEST_SOURCES
from app.modules.scrape.service.source_prefs import user_enabled_sources

logger = structlog.get_logger()


# Scan history — one ScanRun row per executed run


@contextmanager
def record_scan_run(user_id: int, kind: str, *, trigger: str = "auto") -> Iterator[ScanRun]:
    """Open a `ScanRun` row around a scan, stamping the outcome on exit.

    Usage::

        with record_scan_run(user.id, "scrape", trigger="manual") as run:
            result = scrape_for_user(user)
            run.apply_result(result)

    The row is written even when the scan fails (status="error"), because a
    failed run is exactly the thing a user needs to see in "last scan". The
    exception is always re-raised so Celery's retry logic is unaffected.

    Two commits per run — negligible next to the ~100 the run itself does.
    """
    run = ScanRun(
        user_id=user_id,
        kind=kind,
        trigger=trigger,
        status="running",
        started_at=datetime.now(UTC),
    )
    db.session.add(run)
    db.session.commit()

    started = time.monotonic()
    try:
        yield run
    except Exception as exc:
        run.status = "error"
        run.details = {**(run.details or {}), "error": type(exc).__name__}
        _finish_scan_run(run, started)
        raise
    if run.status == "running":  # caller never called apply_result
        run.status = "ok"
    _finish_scan_run(run, started)


def _finish_scan_run(run: ScanRun, started: float) -> None:
    run.finished_at = datetime.now(UTC)
    run.duration_ms = int((time.monotonic() - started) * 1000)
    db.session.commit()


def apply_scan_result(run: ScanRun, result: dict) -> None:
    """Fold a `scrape_for_user` / `link_relevant_feed_items` summary dict into
    an open run row.

    Status vocabulary:
      * "skipped" — the scan short-circuited (no keywords, no sources, nothing
        new to score). Not a failure, but not a real scan either, so it must
        not be reported to the user as "last scan".
      * "partial" — at least one source errored (the -1 sentinel).
      * "ok"      — everything that ran, ran.
    """
    sources = result.get("sources") or {}
    run.hits = int(result.get("hits") or 0)
    run.new_items = int(result.get("linked") or result.get("new") or 0)
    run.source_count = len(sources)
    run.details = {k: v for k, v in result.items() if k in ("sources", "reason")} or None
    if result.get("reason"):
        run.status = "skipped"
    elif any(isinstance(v, int) and v < 0 for v in sources.values()):
        run.status = "partial"
    else:
        run.status = "ok"


def last_scan_run(
    user: User, kind: str = "scrape", *, only_finished: bool = True
) -> ScanRun | None:
    q = ScanRun.query.filter_by(user_id=user.id, kind=kind)
    if only_finished:
        q = q.filter(ScanRun.finished_at.isnot(None))
    return q.order_by(desc(ScanRun.started_at)).first()


#: Rough per-source cost used only until a user has a real run to measure.
#: Self-correcting: after the first scan we use that user's own median.
_COST_SECONDS = {
    "arxiv": 8.0,
    "semantic_scholar": 1.5,
    "pubmed": 3.0,
    "openalex": 2.0,
    "crossref": 2.5,
    # One request each (CQL `or` / PatentsView `_or`), but OPS is markedly
    # slower than the metadata APIs and pays for an OAuth round trip on a cold
    # token — hence the widest estimate of the set.
    "epo_ops": 6.0,
    "patentsview": 3.0,
}


_COST_PER_FEED = 1.5


_COST_LLM = 15.0


def feed_cost_estimate(active_feed_count: int) -> timedelta:
    """How much time this user's custom feeds add to each scan.

    Deliberately *not* routed through `scan_status_context`: that resolves the
    user's effective sources, which can trigger `classify_user_topics` (an LLM
    call). The feed panel re-renders on every toggle, so it must stay cheap.
    """
    return timedelta(seconds=round(_COST_PER_FEED * max(0, active_feed_count)))


def _estimate_scan_seconds(user: User, sources: dict, keyword_count: int, feed_count: int) -> int:
    """Median of this user's recent successful runs, or a static estimate.

    The sources in `_PER_KEYWORD_REQUEST_SOURCES` issue one request per
    keyword, and each active custom feed is one more HTTP round trip — which
    is exactly why this number is worth showing: it makes the cost of "I added
    40 feeds" visible to the person who added them.
    """
    recent = (
        ScanRun.query.filter(
            ScanRun.user_id == user.id,
            ScanRun.kind == "scrape",
            ScanRun.status.in_(("ok", "partial")),
            ScanRun.duration_ms.isnot(None),
        )
        .order_by(desc(ScanRun.started_at))
        .limit(5)
        .all()
    )
    durations = sorted(r.duration_ms for r in recent)
    if durations:
        median_ms = durations[len(durations) // 2]
        return max(1, round(median_ms / 1000))

    total = 0.0
    for name in sources:
        # Keyed off the same set the term-expansion logic uses, so adding a
        # per-keyword source in one place doesn't leave its estimate flat here.
        if name in _PER_KEYWORD_REQUEST_SOURCES:
            total += _COST_SECONDS.get(name, 0.0) * max(1, keyword_count)
        else:
            total += _COST_SECONDS.get(name, 0.0)
    total += _COST_PER_FEED * feed_count
    if feed_count or sources:
        total += _COST_LLM
    return max(1, round(total))


def scan_status_context(user: User) -> dict:
    """Everything the UI needs to state — rather than guess — when this user
    was last scanned and when they will be next.

    Replaces two fictions: a hardcoded "every night at 03:15" string, and a
    "last update" that was really `max(UserPaper.created_at)` (which goes
    stale whenever a scan legitimately finds nothing new).
    """
    from app.tasks.schedule_info import next_scan_at_for_user

    feeds = list_user_feeds(user)
    active_feeds = [f for f in feeds if f.active]
    sources = user_enabled_sources(user)
    keyword_count = len(list_user_keywords(user))

    # Close out anything a dead worker left open BEFORE reading state, so a
    # crashed run can never render as "still scanning" forever.
    reap_stale_runs(user)

    active_run = open_scan_run(user, "scrape")
    last_run = last_scan_run(user, "scrape")

    try:
        next_run = next_scan_at_for_user(user)
    except Exception:  # noqa: BLE001 — beat not configured; the UI degrades
        logger.warning("next_scan_lookup_failed", user_id=user.id)
        next_run = None

    estimated = _estimate_scan_seconds(user, sources, keyword_count, len(active_feeds))

    return {
        "last_run": last_run,
        "next_run_at": next_run,
        # "Running" means a task actually started and wrote a row — NOT that a
        # lock is held. The lock is taken by the HTTP route before the task is
        # picked up, so keying the UI on it claimed "scanning" for 15 minutes
        # even when no worker ever ran (and never updated on its own).
        "is_running": active_run is not None,
        "active_run": active_run,
        "running_seconds": (
            None
            if active_run is None
            else max(0, int((datetime.now(UTC) - aware(active_run.started_at)).total_seconds()))
        ),
        "estimated_seconds": estimated,
        # Templates render this through Babel's `timedeltaformat` so the
        # duration is localised ("2 dakika" / "2 minutes") without a custom filter.
        "estimated_delta": timedelta(seconds=estimated),
        "user_active_sources": sorted(sources.keys()),
        "feed_count": len(feeds),
        "active_feed_count": len(active_feeds),
        "max_user_feeds": current_app.config.get("MAX_USER_FEEDS", 50),
    }


def aware(dt: datetime) -> datetime:
    """Postgres gives us aware values, SQLite/in-memory rows may not."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def open_scan_run(user: User, kind: str = "scrape") -> ScanRun | None:
    """The run that is genuinely in flight right now, if any.

    Call `reap_stale_runs` first (or use `scan_status_context`, which does):
    on its own this would also return rows abandoned by a killed worker.
    """
    return (
        ScanRun.query.filter(
            ScanRun.user_id == user.id,
            ScanRun.kind == kind,
            ScanRun.finished_at.is_(None),
        )
        .order_by(desc(ScanRun.started_at))
        .first()
    )


def reap_stale_runs(user: User | None = None) -> int:
    """Close out runs whose worker died mid-scan.

    A task that is SIGKILLed past its hard time limit never gets to stamp
    `finished_at`, so without this the row stays open and the dashboard shows
    "scanning…" indefinitely — the exact failure mode that made the old
    indicator untrustworthy. Anything older than the hard time limit (plus a
    small grace) cannot still be running, by definition.

    Returns the number of rows closed. Cheap and idempotent, so it runs on
    every status read as well as in the nightly purge task.
    """
    limit = int(current_app.config.get("CELERY_TASK_TIME_LIMIT", SCRAPE_LOCK_TTL))
    cutoff = datetime.now(UTC) - timedelta(seconds=limit + 60)
    q = ScanRun.query.filter(ScanRun.finished_at.is_(None), ScanRun.started_at < cutoff)
    if user is not None:
        q = q.filter(ScanRun.user_id == user.id)

    stale = q.all()
    for run in stale:
        run.status = "error"
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((run.finished_at - aware(run.started_at)).total_seconds() * 1000)
        run.details = {**(run.details or {}), "error": "abandoned"}
    if stale:
        db.session.commit()
        logger.warning("scan_runs_reaped", count=len(stale))
    return len(stale)


def purge_scan_runs(older_than_days: int) -> int:
    """Delete run rows older than `older_than_days`. 0 disables purging.
    Mirrors `core.purge_audit_logs`."""
    if older_than_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    deleted = ScanRun.query.filter(ScanRun.started_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return int(deleted or 0)
