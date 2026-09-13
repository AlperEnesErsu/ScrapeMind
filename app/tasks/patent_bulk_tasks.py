"""Weekly USPTO bulk load (Faz 8.3).

Distinct from `patent_tasks` in every dimension that matters: that one scans
*per user* against metered third-party APIs and records a `ScanRun`; this one
is system work with no user attached, pulls one public file, and records a
`PatentIngestRun`. Sharing either the task family or the run table would make
"your last scan" answer a question no user asked.

No fan-out, no per-user lock. What it needs instead is a guard against two
loads overlapping — a weekly file takes minutes to parse and Beat firing
while a manual run is in flight would have both writing the same rows. The
Redis lock is that guard.

Beat fires this Wednesday 04:45 — USPTO publishes Tuesdays, and 04:45 sits
after the three nightly purges (04:00/04:15/04:30) so it never contends with
them. That time is `BABEL_DEFAULT_TIMEZONE` (Europe/Istanbul), not UTC; see
`app/tasks/schedule.py`.
"""

from __future__ import annotations

import structlog

from app.tasks import celery_app

logger = structlog.get_logger()

_LOCK_KEY = "patents_bulk:refresh"
#: Longer than a load should ever take, short enough that a killed worker
#: does not block next week's run.
_LOCK_TTL = 60 * 60 * 3


def _lock():
    """Redis lock, or None when Redis is unavailable.

    A missing Redis must not take the load down — it removes the overlap
    guard, and a single deployment running one Beat is not where overlap
    comes from.
    """
    try:
        from app.modules.scrape.ratelimit import _client

        return _client()
    except Exception:  # noqa: BLE001 - lock is best-effort by design
        return None


@celery_app.task(name="patents_bulk.refresh_window", bind=True, max_retries=1)
def refresh_window(self, *, limit: int | None = None) -> dict:
    """Discover, download, ingest and purge — one weekly cycle.

    Retries once: the failure this guards against is a transport blip on a
    large download, and a second attempt reuses the partially-established
    state (an already-downloaded archive is not re-fetched). A parse failure
    will fail the same way twice, which is the correct outcome — it needs a
    person, not another attempt.
    """
    from app.modules.patent.ingest import refresh_window as run_cycle
    from app.modules.patent.uspto import CredentialsMissingError

    client = _lock()
    if client is not None and not client.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        logger.info("patents_bulk_already_running")
        return {"status": "skipped", "reason": "already_running"}

    try:
        result = run_cycle(limit=limit)
        # Queued, not called: embedding is billable and belongs to the `llm`
        # pool, and a slow provider must not hold this I/O worker or the lock.
        # Not after a skipped load -- nothing new arrived to embed.
        if result.get("status") != "skipped":
            embed_pending.delay()
        return result
    except CredentialsMissingError:
        # Configuration, not a blip: a retry in five minutes finds the same
        # missing key. `run_cycle` checks first, so this is a race with the key
        # being removed mid-run.
        raise
    except Exception as exc:  # noqa: BLE001 - retried once, then surfaced
        logger.warning("patents_bulk_refresh_failed", error=str(exc))
        raise self.retry(exc=exc, countdown=300) from exc
    finally:
        if client is not None:
            client.delete(_LOCK_KEY)


@celery_app.task(name="patents_bulk.purge_window")
def purge_window() -> dict:
    """Drop what has fallen out of the window.

    Separate from `refresh_window` so the window keeps shrinking correctly on
    a deployment where the download is broken or switched off. The corpus
    going stale is acceptable; the corpus growing without bound is not.
    """
    from app.modules.patent.ingest import purge_window as run_purge

    return {"removed": run_purge()}


@celery_app.task(name="patents_bulk.embed_pending")
def embed_pending(limit: int | None = None) -> dict:
    """Claim-1 vectors for documents that lack one from the current model.

    Queued by `refresh_window` after each load, and on its own daily as a
    catch-up: a provider outage during the weekly run would otherwise leave
    that week's patents out of semantic search until the next one.
    """
    from app.modules.patent.embedding import embed_pending as run_embed

    return run_embed(limit=limit)
