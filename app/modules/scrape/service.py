"""Persistence layer for scraped papers.

The service deduplicates incoming payloads against (source, external_id)
and keeps a per-user record of which keyword surfaced what — that's how
the "For you" dashboard becomes a personal feed instead of a firehose.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import requests
import structlog
from flask import current_app
from flask_babel import gettext as _
from flask_babel import lazy_gettext as _l
from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db
from app.modules.academic.service import list_user_keywords
from app.modules.scrape.doi import normalize_doi
from app.modules.scrape.models import (
    Journal,
    Paper,
    PaperNote,
    ScanRun,
    UserAuthor,
    UserChannel,
    UserFeed,
    UserPaper,
    UserSource,
)
from app.modules.scrape.net_guard import is_public_http_url
from app.modules.scrape.sources import (
    SOURCE_META,
    admin_optin_key,
    enabled_sources,
)
from app.modules.scrape.sources.payload import PaperPayload
from app.modules.scrape.sources.rss_source import fetch_feed_conditional

logger = structlog.get_logger()

#: Shown when a user tries to add feed number MAX_USER_FEEDS + 1. Kept as a
#: module constant so the route, the template and the tests agree on it. No
#: placeholder: the route passes it straight to `_()`, and the panel header
#: already shows the "42 / 50" count.
FEED_CAP_MESSAGE = _l("Feed limit reached. Remove one before adding another.")


# ----------------------------------------------------------------------------
# Per-user source selection (opt-out; no row == enabled)
# ----------------------------------------------------------------------------


def admin_opted_in(setting_key: str) -> bool:
    """Whether an admin has switched on the `requires_admin_optin` gate named
    by `setting_key` (Faz 5.1).

    Defaults to False — a licensed or metered source stays off until someone
    with `system.manage` deliberately turns it on. A failed read is False for
    the same reason `ratelimit.consume_quota` is fail-closed: when the answer
    is "we can't tell", not calling a paid API is the safe direction.
    """
    from app.core.settings.service import get_system_setting

    try:
        return bool(get_system_setting(setting_key, False))
    except Exception:  # noqa: BLE001 — see the docstring: fail closed
        logger.warning("admin_optin_read_failed", setting_key=setting_key)
        return False


def list_user_source_prefs(user: User) -> dict[str, bool]:
    """Map of source_name -> enabled for this user's explicit choices only.
    Sources without a row are absent here (they default to enabled)."""
    rows = UserSource.query.filter_by(user_id=user.id).all()
    return {r.source_name: r.enabled for r in rows}


def effective_source_prefs(user: User, *, user_topics: list[str] | None = None) -> dict[str, bool]:
    """Effective enabled/disabled state for every deployment-enabled source,
    folding the interest-aware default (Faz 3 Bölüm C) in under any explicit
    `UserSource` override.

    Resolution order per source:
      0. `requires_admin_optin` and the admin has not opted in — off, and the
         explicit row in step 1 does not get to override it (Faz 5.1). This
         gate is a deployment-level licensing/cost decision, so it outranks
         user preference; a stale `UserSource` row from before the admin
         turned the source off must not keep scraping it.
      1. Explicit `UserSource` row — always wins, whichever way it's set.
      2. No row + `category == "academic"` / `"patent"` — on (unchanged Faz 1/2
         behaviour; these databases are relevant regardless of specialty)…
         except for a source still carrying an admin opt-in, which is
         **default off** even once opted into: someone paying per request
         should turn it on deliberately, not discover it in a bill.
      3. No row + `category == "feed"` — on only if the source's `topics`
         intersect `classify_user_topics(user)` (Faz 3's actual point: a
         history researcher doesn't get AI industry feeds default-on).

    `user_topics` can be passed in by a caller that already classified this
    user this request (e.g. the sources-card context builder) to skip a
    redundant (if cheap/cached) lookup; otherwise it's resolved lazily, and
    only when step 3 is actually reached (i.e. the user has at least one
    un-overridden feed source).
    """
    prefs = list_user_source_prefs(user)
    out: dict[str, bool] = {}
    suggested: set[str] | None = None
    # One SystemSettings read per distinct gate, not per gated source.
    optin_cache: dict[str, bool] = {}
    for name in enabled_sources():
        optin_key = admin_optin_key(name)
        if optin_key is not None:
            if optin_key not in optin_cache:
                optin_cache[optin_key] = admin_opted_in(optin_key)
            if not optin_cache[optin_key]:
                out[name] = False
                continue
        if name in prefs:
            out[name] = prefs[name]
            continue
        if optin_key is not None:
            out[name] = False
            continue
        meta = SOURCE_META.get(name, {})
        if meta.get("category") != "feed":
            out[name] = True
            continue
        if suggested is None:
            from app.modules.scrape.ai_service import classify_user_topics
            from app.modules.scrape.sources import suggested_sources

            if user_topics is None:
                user_topics = classify_user_topics(user)
            suggested = suggested_sources(user_topics)
        out[name] = name in suggested
    return out


def sources_card_context(user: User) -> dict:
    """Interest-aware source picker context (Faz 3 Bölüm C) — shared by the
    dashboard index, its HTMX partial re-render, and the papers feed sidebar.
    Groups the deployment's enabled sources into "suggested for you" (topics
    overlap the user's classified interests) vs "other", and folds explicit
    `UserSource` overrides in for each source's on/off toggle state.

    Lives here (not in the dashboard module) because scrape owns sources —
    dashboard just renders this card, it never computes source state itself.
    """
    from app.modules.scrape.ai_service import classify_user_topics
    from app.modules.scrape.sources import source_options, suggested_sources

    sources = source_options()
    user_topics = classify_user_topics(user)
    prefs = effective_source_prefs(user, user_topics=user_topics)
    suggested_names = suggested_sources(user_topics)

    for opt in sources:
        opt["is_on"] = prefs.get(opt["name"], True)
        opt["is_suggested"] = opt["name"] in suggested_names

    # Own-channel counts, for the card's "Your feeds" summary block — mirrors
    # feed_count/active_feed_count, which come from scan_status_context, not
    # here; kept together with the channel counts so both dashboard.index and
    # scrape.feed (the two callers that render _sources_card.html) get them
    # for free by merging this context in, same as they already do for feeds.
    channels = list_user_channels(user)
    active_channel_count = sum(1 for c in channels if c.active)
    active_curated_count = sum(1 for s in sources if s["is_on"])

    # `active_source_count` (below) undercounts "what will actually be
    # scanned for me": it's the curated deployment sources only, and never
    # included the user's own RSS feeds or YouTube channels even though both
    # are scanned every run. `active_feed_count` normally comes from
    # scan_status_context, not here, but the honest total needs it — one more
    # cheap indexed query beats leaning on the caller to add three numbers
    # together in a template (the "Your feeds" badge above already does that
    # arithmetic in Jinja; this key exists so the home-page pill doesn't have
    # to).
    active_feed_count = sum(1 for f in list_user_feeds(user) if f.active)

    return {
        "sources": sources,
        "suggested_sources": [s for s in sources if s["is_suggested"]],
        "other_sources": [s for s in sources if not s["is_suggested"]],
        "active_source_count": active_curated_count,
        "user_topics": user_topics,
        "channel_count": len(channels),
        "active_channel_count": active_channel_count,
        "scanned_source_count": active_curated_count + active_feed_count + active_channel_count,
    }


def user_enabled_sources(user: User):
    """The deployment's enabled sources, filtered to this user's effective
    preferences — explicit `UserSource` overrides win; otherwise academic
    sources default on and feed sources default on only when their topics
    match the user's interests (see `effective_source_prefs`)."""
    prefs = effective_source_prefs(user)
    return {name: mod for name, mod in enabled_sources().items() if prefs.get(name, True)}


# ----------------------------------------------------------------------------
# Per-user run locks — collapse duplicate presses / overlapping nightly runs
# ----------------------------------------------------------------------------

SCRAPE_LOCK_TTL = 900  # seconds — safety expiry if a worker dies mid-run


def _lock_client():
    """Best-effort redis client on the Celery broker. Returns None (→ fail-open)
    when redis is unset/unreachable, so locking never blocks scraping."""
    url = current_app.config.get("CELERY_BROKER_URL") or current_app.config.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis  # noqa: PLC0415 — optional dependency

        client = redis.Redis.from_url(
            url, socket_timeout=0.25, socket_connect_timeout=0.25, decode_responses=True
        )
        client.ping()
        return client
    except Exception:  # noqa: BLE001 — no redis means "no lock", not "no scrape"
        return None


def _lock_key(user_id: int, kind: str) -> str:
    return f"scan:lock:{kind}:{user_id}"


def acquire_user_lock(user_id: int, kind: str = "scrape", *, ttl: int | None = None) -> bool:
    """Try to claim this user's slot for `kind` ("scrape" | "feeds").

    Returns True if claimed (caller may proceed), False if a run of the same
    kind is already queued/running for this user. Fail-open: if redis is
    unavailable we return True — never block work over a missing lock.

    The default TTL follows the hard task time limit so the lock can't outlive
    a worker that was killed mid-run.
    """
    client = _lock_client()
    if client is None:
        return True
    if ttl is None:
        ttl = int(current_app.config.get("CELERY_TASK_TIME_LIMIT", SCRAPE_LOCK_TTL))
    try:
        return bool(client.set(_lock_key(user_id, kind), "1", nx=True, ex=ttl))
    except Exception:  # noqa: BLE001
        return True


def release_user_lock(user_id: int, kind: str = "scrape") -> None:
    """Free this user's slot for `kind` (called by the task when it finishes)."""
    client = _lock_client()
    if client is None:
        return
    try:
        client.delete(_lock_key(user_id, kind))
    except Exception:  # noqa: BLE001
        pass


# Note: there is deliberately no "is this user locked?" helper for the UI.
# The lock means "queued or running" and is claimed by the HTTP route before a
# worker touches the task, so a deployment with no worker would hold it for the
# full TTL and the dashboard would claim to be scanning the whole time. The UI
# reads `open_scan_run` instead — a row only exists once a task really started.


# Kept so the manual-scrape route and its tests keep working unchanged.
def acquire_scrape_lock(user_id: int) -> bool:
    return acquire_user_lock(user_id, "scrape")


def release_scrape_lock(user_id: int) -> None:
    release_user_lock(user_id, "scrape")


# ----------------------------------------------------------------------------
# Scan history — one ScanRun row per executed run
# ----------------------------------------------------------------------------


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


def set_user_source(user: User, source_name: str, enabled: bool) -> bool:
    """Upsert this user's preference for one source. Idempotent. Returns the
    stored enabled value."""
    row = UserSource.query.filter_by(user_id=user.id, source_name=source_name).first()
    if row is None:
        row = UserSource(user_id=user.id, source_name=source_name, enabled=bool(enabled))
        db.session.add(row)
    else:
        row.enabled = bool(enabled)
    db.session.commit()
    return row.enabled


#: Fields that are safe to backfill on a matched row. Deliberately excludes
#: `title`, `source`, `external_id`, `kind` — those identify the row (or, for
#: title, are never blank in practice) and overwriting them on a match would
#: make upsert_paper silently rewrite identity rather than fill gaps.
_ENRICHABLE_FIELDS = (
    "abstract",
    "pdf_url",
    "url",
    "doi",
    "published_at",
    "categories",
    "authors",
    # A paper's journal does not change; only OpenAlex/Crossref report it, so
    # every other source picks it up for free through a DOI match.
    "issn_l",
)


def _is_empty(value: object) -> bool:
    """True for the "nothing here yet" shapes of our enrichable fields:
    None, empty string, empty list. Deliberately NOT true for falsy-but-
    meaningful values like 0 or False — none of the enrichable fields are
    numeric/boolean today, but this keeps the helper honest if one ever is.
    """
    if value is None:
        return True
    if isinstance(value, (str, list, tuple)):
        return len(value) == 0
    return False


#: Fields that are *refreshed* on every match rather than filled once.
#:
#: `cited_by_count` is a running total, not a fact about the paper: a 2019
#: article picks up citations for years. Under the fill-only rule it would
#: freeze at whatever the first source happened to report — showing "12
#: citations" forever for a paper that now has 300, which is worse than
#: showing nothing, because it looks precise.
#:
#: Keep this set small and obviously time-varying. Everything else belongs in
#: `_ENRICHABLE_FIELDS`, where "we have no basis to pick a winner between two
#: non-empty values" is the right default.
_REFRESHABLE_FIELDS = ("cited_by_count",)


def _enrich(paper: Paper, data: dict) -> bool:
    """Merge `data` onto an existing `paper` row.

    Two rules, deliberately different:

      * `_ENRICHABLE_FIELDS` — **fill-only**. Write the incoming value only if
        the existing field is empty and the incoming one is not. A populated
        field is never overwritten, even by a different-but-also-non-empty
        value from another source: with both non-empty we have no basis for
        deciding which source is right, so whatever arrived first stays.
      * `_REFRESHABLE_FIELDS` — **always updated** when the incoming value is
        present, because the field tracks something that changes over time.

    Returns whether anything actually changed, so the caller can skip a no-op
    commit.
    """
    changed = False
    for field in _ENRICHABLE_FIELDS:
        if field not in data:
            continue
        incoming = data[field]
        if _is_empty(incoming):
            continue
        if not _is_empty(getattr(paper, field)):
            continue
        setattr(paper, field, incoming)
        changed = True

    for field in _REFRESHABLE_FIELDS:
        if field not in data:
            continue
        incoming = data[field]
        # An absent count is "this source didn't say", not "zero citations" —
        # so a source that omits the field must not wipe a real number.
        if incoming is None:
            continue
        if getattr(paper, field) == incoming:
            continue
        setattr(paper, field, incoming)
        changed = True
    return changed


def upsert_paper(payload: PaperPayload | dict) -> Paper:
    """Insert a paper if we haven't seen it before; otherwise return the
    existing row, enriched with anything new the payload can fill in.

    Resolution order: normalized DOI, then `(source, external_id)`. The DOI
    is normalized on write (see `doi.normalize_doi`) so the stored form is
    always canonical and lookup is a plain `filter_by(doi=...)` rather than
    an `ilike` scan. The earlier version stored whatever string a source
    handed it and matched with `ilike`, which is case-insensitive but not
    prefix-insensitive — so "https://doi.org/10.X/Y" from one source and
    "10.X/Y" from another still created two rows.

    Enrichment is fill-only (see `_enrich`): a matched row's empty fields
    (abstract, pdf_url, url, doi, published_at, categories, authors) are
    backfilled from the new payload, but a populated field is never
    overwritten. This is deliberately conservative — we have no basis for
    picking "which source is right" when both already have a value, so we
    just keep whatever was there first.

    Edge case worth documenting: if the incoming DOI matches one existing
    row but the incoming (source, external_id) matches a *different* row
    (e.g. a stale/wrong DOI was recorded on that second row previously), the
    DOI match wins and is the one enriched — the other row is left
    untouched. Merging the two rows (e.g. moving UserPaper links across) is
    out of scope here; it would need a real migration, not an upsert.

    Race safety: the SELECT above and the INSERT below are not atomic, so two
    workers can both miss the SELECT for the same brand-new (source,
    external_id) — e.g. the same arXiv paper matching two different users'
    keywords in concurrent nightly scans — and both fall through to insert.
    Without `ON CONFLICT`, the loser's plain INSERT would raise
    `IntegrityError` against `uq_paper_source_external` and force Celery's
    retry machinery to clean up noise that was never a real failure. Instead
    the loser's insert becomes a no-op and it re-selects the winner's row
    (then enriches it with anything its own payload had that the winner's
    didn't) — same fill-only semantics as the plain-existing-row path above,
    just reached one step later.
    """
    data = payload.as_dict() if isinstance(payload, PaperPayload) else dict(payload)
    if "doi" in data:
        data["doi"] = normalize_doi(data["doi"])

    existing = None
    doi = data.get("doi")
    if doi:
        existing = Paper.query.filter_by(doi=doi).first()
    if existing is None:
        existing = Paper.query.filter_by(
            source=data["source"], external_id=data["external_id"]
        ).first()

    if existing is not None:
        if _enrich(existing, data):
            db.session.commit()
        return existing

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(Paper.__table__)
        .values(**data)
        .on_conflict_do_nothing(index_elements=["source", "external_id"])
    )
    db.session.execute(stmt)
    db.session.commit()

    paper = Paper.query.filter_by(source=data["source"], external_id=data["external_id"]).first()
    if paper is None:  # pragma: no cover — the unique index guarantees a row either way
        raise RuntimeError("upsert_paper: insert conflicted but no row was found")
    if _enrich(paper, data):
        db.session.commit()
    return paper


def link_user_paper(
    user: User, paper: Paper, *, matched_keyword: str | None
) -> tuple[UserPaper, bool]:
    """Idempotent. Returns (link, created) — created=True only on first insert."""
    existing = UserPaper.query.filter_by(user_id=user.id, paper_id=paper.id).first()
    if existing is not None:
        return existing, False
    link = UserPaper(user_id=user.id, paper_id=paper.id, matched_keyword=matched_keyword)
    db.session.add(link)
    db.session.commit()
    return link, True


def add_paper_from_url(user: User, url: str) -> tuple[UserPaper, bool]:
    """Manually add a paper from a public web URL using net_guard + web_reach.
    Returns (UserPaper link, created boolean). Raises ValueError on validation/net_guard error.
    """
    clean_url = (url or "").strip()
    if not clean_url.startswith(("http://", "https://")):
        raise ValueError(_("Invalid URL. Must start with http:// or https://"))

    allow_private = False
    try:
        from flask import current_app

        allow_private = bool(current_app.config.get("FEED_ALLOW_PRIVATE_HOSTS", False))
    except Exception:  # noqa: BLE001
        pass

    from app.modules.scrape.net_guard import is_public_http_url

    ok, _err = is_public_http_url(clean_url, allow_private=allow_private)
    if not ok:
        raise ValueError(_("URL blocked by security guard (private or loopback IP)"))

    from app.modules.scrape.sources.external_sources import search_web

    payloads = search_web(clean_url)
    if not payloads:
        raise ValueError(_("Failed to fetch web content from URL"))

    payload = replace(payloads[0], source="manual", kind="link", categories=["manual"])

    # The cleaned reader text still opens on page chrome (language switchers,
    # badges, install banners) that no regex can reliably tell from content,
    # so ask the LLM for a real summary. Best-effort: a failure here must not
    # cost the user the link, so we keep the cleaned text on any error.
    if payload.abstract:
        try:
            from app.modules.scrape.ai_service import summarize_web_content

            summary = summarize_web_content(payload.title, payload.abstract, user=user)
            if summary:
                payload = replace(payload, abstract=summary)
        except Exception:  # noqa: BLE001 — summary is a nicety, the link is the point
            logger.warning("manual_link_summary_failed", url=clean_url, user_id=user.id)

    paper = upsert_paper(payload)
    # `matched_keyword` is a stored column, so a translated label would freeze
    # the row into whichever language was active at insert time. Manually
    # added rows are identified by `source == "manual"` and labelled at render.
    link, created = link_user_paper(user, paper, matched_keyword=None)
    return link, created


def _match_keyword(title: str, terms: list[str], alias: dict[str, str] | None = None) -> str:
    """Best-effort attribution: first term whose tokens all appear in the
    title — good enough for analytics, perfect for v1.

    `alias` maps a lowercased *search* term back to the user's own keyword, so
    a paper found via the English expansion is still filed under the Turkish
    interest the user actually follows. Without it the feed would show
    "heart failure" next to a keyword the user never typed.
    """
    lowered = title.lower()
    alias = alias or {}
    hit = next(
        (t for t in terms if all(tok in lowered for tok in t.lower().split())),
        terms[0],
    )
    return alias.get(hit.lower(), hit)


#: Sources that issue one HTTP request *per keyword* (no OR operator in their
#: API). Handing them the expanded term list would multiply their request
#: count — and their rate limit is the tightest we deal with — so they get the
#: canonical English form only. arXiv and PubMed OR-combine everything into a
#: single request, where extra terms are free. Crossref's `query.bibliographic`
#: is the same story as Semantic Scholar's relevance search — bag-of-words
#: scoring with no boolean OR — so it belongs in this set too.
_PER_KEYWORD_REQUEST_SOURCES = {"semantic_scholar", "crossref"}


def ensure_keyword_translations(keywords: list, *, user: User | None = None) -> int:
    """Fill in `value_en`/`variants` for any keyword row that has none yet.
    Returns how many rows were filled.

    Called at the top of a scan rather than when the user adds a keyword: the
    add path is a synchronous HTMX request and must not wait on an LLM. Terms
    the translator can't resolve are left alone (no `translated_at`), so they
    retry on the next scan — that's the case where AI simply isn't configured
    yet, and it starts working the moment it is.

    `keywords` are `academic.models.Keyword` rows, typed loosely to keep this
    module's imports one-directional.
    """
    pending = [kw for kw in keywords if not (kw.value_en or "").strip()]
    if not pending:
        return 0

    from app.modules.scrape.ai_service import translate_keywords

    try:
        resolved = translate_keywords([kw.value for kw in pending], user=user)
    except Exception:  # noqa: BLE001 — translation is an optimisation, never a blocker
        logger.exception("keyword_translation_failed", user_id=getattr(user, "id", None))
        return 0

    filled = 0
    now = datetime.now(UTC)
    for kw in pending:
        hit = resolved.get(kw.value)
        if not hit:
            continue
        kw.value_en = hit["en"]
        kw.variants = hit["variants"] or None
        kw.translated_at = now
        filled += 1
    if filled:
        db.session.commit()
        logger.info("keyword_translations_stored", filled=filled, pending=len(pending))
    return filled


def keyword_search_terms(keywords: list, source_name: str) -> tuple[list[str], dict[str, str]]:
    """Terms to query `source_name` with, plus a lowercased term -> original
    keyword map for attribution.

    Per-request-per-keyword sources get one term each (English if we have it);
    everything else gets the full expansion. Deduplicated across keywords, so
    two interests that translate to the same English term don't double the
    query.
    """
    per_request = source_name in _PER_KEYWORD_REQUEST_SOURCES
    terms: list[str] = []
    alias: dict[str, str] = {}
    for kw in keywords:
        expansion = kw.search_terms if hasattr(kw, "search_terms") else [kw.value]
        if per_request:
            expansion = expansion[:1]
        for term in expansion:
            key = term.lower()
            if key in alias:
                continue
            alias[key] = kw.value
            terms.append(term)
    return terms, alias


def _hydrate_scopus_payloads(payloads: list) -> list:
    """Replace Scopus discovery payloads with OpenAlex records where possible.

    This is the working half of the discovery-only design
    (`docs/adr/0002-elsevier-discovery-only.md`): Scopus tells us a DOI exists,
    OpenAlex supplies the metadata we are allowed to keep. A hydrated payload
    is returned under `source="openalex"`, so what lands in `papers` is an
    OpenAlex record — abstract, authors, ISSN, citations and all.

    A DOI OpenAlex has never heard of keeps its Scopus payload: a title and a
    link-out, no abstract. That is the residual exposure the ADR names, and it
    is bounded to the title.

    One request per DOI, which is why Scopus is capped at 25 results a run.
    A hydration failure is logged and the discovery payload kept — losing the
    result entirely would be worse than showing a bare title.
    """
    from app.modules.scrape.sources.openalex_source import fetch_by_doi

    out = []
    for payload in payloads:
        if not payload.doi:
            out.append(payload)
            continue
        try:
            hydrated = fetch_by_doi(payload.doi)
        except Exception:  # noqa: BLE001 — a miss must not lose the result
            logger.warning("scopus_hydration_failed", doi=payload.doi)
            hydrated = None
        out.append(hydrated or payload)
    return out


#: Wait before the single retry in `_search_with_transient_retry`. A module
#: constant (not a literal in the loop) so a test can monkeypatch it to 0
#: instead of a real scan actually pausing 5 seconds.
TRANSIENT_RETRY_DELAY_SECONDS = 5.0


def _search_with_transient_retry(
    source, terms: list[str], *, max_results: int, source_name: str, user_id: int
):
    """Call `source.search_for_keywords`, retrying exactly once on a
    transient network failure.

    `scrape_for_user` isolates each source behind a broad try/except (see its
    docstring) so a source's exception never reaches the Celery task — which
    also means the task's own retry (60/240s backoff) never gets a chance to
    run for it. Without this, a `ConnectionError` from a one-second network
    blip costs that source the rest of the night, not just one attempt.

    Deliberately narrow:
      * `requests.exceptions.ConnectionError` / `Timeout` only — the two
        shapes an actual network hiccup takes.
      * No retry for `requests.exceptions.HTTPError` (a 4xx is a permanent
        rejection; retrying wastes a request against a source with a rate
        limit) or `SourceThrottledError` (already means "back off" — an
        immediate retry would defeat the point). Both fall straight through
        to the caller's except-and-sentinel handling, unchanged.
      * One retry, not a loop — a source that's still down 5 seconds later is
        down for the night, and the caller already isolates that outcome.
    """
    try:
        return source.search_for_keywords(terms, max_results=max_results)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        logger.warning("scrape_source_transient_retry", source=source_name, user_id=user_id)
        time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
        return source.search_for_keywords(terms, max_results=max_results)


def scrape_for_user(user: User, *, max_results: int = 25) -> dict:
    """Run every enabled source with this user's keywords; persist + link the
    results back to them.

    Keywords are expanded to their English form first (see
    `ensure_keyword_translations`) because every source we query is an English
    corpus — a user following "kalp yetmezliği" got zero hits before this.
    The expansion is per-source: see `_PER_KEYWORD_REQUEST_SOURCES`.

    Sources are isolated: one source raising (rate limit, network, API change)
    is logged and skipped so the remaining sources still land. A transient
    connection failure gets one retry first (see
    `_search_with_transient_retry`) so a momentary network blip doesn't cost
    the source the rest of the night. Returns a summary dict with per-source
    hit counts for the calling task.
    """
    keyword_rows = list_user_keywords(user)
    if not keyword_rows:
        logger.info("scrape_skip_no_keywords", user_id=user.id)
        return {"hits": 0, "linked": 0, "reason": "no_keywords"}

    sources = user_enabled_sources(user)
    if not sources:
        logger.info("scrape_skip_no_sources", user_id=user.id)
        return {"hits": 0, "linked": 0, "reason": "no_sources"}

    ensure_keyword_translations(keyword_rows, user=user)

    hits = 0
    linked = 0
    per_source: dict[str, int] = {}
    for name, source in sources.items():
        terms, alias = keyword_search_terms(keyword_rows, name)
        if not terms:
            continue
        try:
            payloads = _search_with_transient_retry(
                source, terms, max_results=max_results, source_name=name, user_id=user.id
            )
        except Exception:  # noqa: BLE001 — a flaky source must not kill the run
            logger.exception("scrape_source_failed", source=name, user_id=user.id)
            per_source[name] = -1  # sentinel: this source errored
            continue
        if name == "scopus":
            payloads = _hydrate_scopus_payloads(payloads)
        per_source[name] = len(payloads)
        hits += len(payloads)
        for payload in payloads:
            paper = upsert_paper(payload)
            _, created = link_user_paper(
                user, paper, matched_keyword=_match_keyword(paper.title, terms, alias)
            )
            if created:
                linked += 1
    logger.info("scrape_done", user_id=user.id, hits=hits, linked=linked, sources=per_source)
    return {"hits": hits, "linked": linked, "sources": per_source}


# Backward-compatible alias — pre-multi-source callers used the arXiv name.
scrape_arxiv_for_user = scrape_for_user


# ----------------------------------------------------------------------------
# Author following (Faz 5.4)
# ----------------------------------------------------------------------------

#: Per-user cap on followed authors. Each active follow is one OpenAlex
#: request per night, so this is a politeness budget against a free API, not a
#: product limit — hence a module constant rather than an admin setting.
MAX_USER_AUTHORS = 50

AUTHOR_CAP_MESSAGE = _l("Author limit reached. Unfollow one before adding another.")


def list_user_authors(user: User) -> list[UserAuthor]:
    return UserAuthor.query.filter_by(user_id=user.id).order_by(UserAuthor.author_name.asc()).all()


def count_user_authors(user: User) -> int:
    return UserAuthor.query.filter_by(user_id=user.id).count()


def follow_author(user: User, raw: str) -> tuple[UserAuthor | None, str | None]:
    """Follow an author by ORCID or OpenAlex id. Returns `(row, None)` or
    `(None, error_message)` — never raises for bad user input.

    Resolution happens here, once, rather than per nightly run: it is the
    expensive and failure-prone half, and doing it on follow means a typo
    surfaces immediately in front of the user instead of disappearing into a
    task log a night later.

    Re-following an author already on the list reactivates a paused row rather
    than creating a duplicate, and consumes no cap slot — same shape as
    `add_user_channel`.
    """
    from app.modules.scrape.sources.openalex_source import fetch_author

    raw = (raw or "").strip()
    if not raw:
        return None, _l("Please enter an ORCID or OpenAlex author id.")

    try:
        resolved = fetch_author(raw)
    except Exception:  # noqa: BLE001 — a lookup failure is user-facing, not a 500
        logger.exception("author_resolve_failed", user_id=user.id)
        return None, _l("Could not reach OpenAlex right now. Please try again.")

    if resolved is None:
        return None, _l("No author found for that ORCID or OpenAlex id.")

    existing = UserAuthor.query.filter_by(user_id=user.id, openalex_id=resolved["id"]).first()
    if existing is not None:
        if not existing.active:
            existing.active = True
            db.session.commit()
        return existing, None

    if count_user_authors(user) >= MAX_USER_AUTHORS:
        logger.info("user_author_cap_reached", user_id=user.id, cap=MAX_USER_AUTHORS)
        return None, AUTHOR_CAP_MESSAGE

    name = resolved["name"] or raw
    # The table's unique constraint is on (user_id, author_name) and predates
    # openalex_id. Two different OpenAlex authors can share a display name, so
    # disambiguate rather than letting the insert fail.
    if UserAuthor.query.filter_by(user_id=user.id, author_name=name).first() is not None:
        name = f"{name} ({resolved['id']})"

    row = UserAuthor(
        user_id=user.id,
        author_name=name[:128],
        openalex_id=resolved["id"],
        orcid=resolved["orcid"],
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    logger.info("author_followed", user_id=user.id, openalex_id=resolved["id"])
    return row, None


def unfollow_author(user: User, author_id: int) -> bool:
    """Delete a follow. Returns whether a row was actually removed."""
    row = UserAuthor.query.filter_by(id=author_id, user_id=user.id).first()
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def toggle_user_author(user: User, author_id: int) -> UserAuthor | None:
    """Pause/resume a follow, keeping the row and its `last_work_at`
    high-water mark — deleting and re-adding would lose the latter and
    re-import the author's back catalogue."""
    row = UserAuthor.query.filter_by(id=author_id, user_id=user.id).first()
    if row is None:
        return None
    row.active = not row.active
    db.session.commit()
    return row


def ingest_user_authors(user: User, *, max_results: int = 25) -> dict:
    """Fetch new works for every author this user follows.

    Returns the same summary shape as the other scan paths (`{"sources": {…},
    "hits": n, "linked": n}`) so `apply_scan_result` reads it unchanged, with
    `-1` marking an author whose lookup failed.

    Each author is asked only for works published since `last_work_at`, which
    is advanced afterwards. A follow with no `openalex_id` (resolution failed
    at some point) is skipped rather than retried here — re-resolving belongs
    in the follow path, where a human can see the error.
    """
    from app.modules.scrape.sources.openalex_source import works_by_author

    authors = [a for a in list_user_authors(user) if a.active and a.openalex_id]
    if not authors:
        return {"hits": 0, "linked": 0, "reason": "no_authors"}

    hits = 0
    linked = 0
    summary: dict[str, int] = {}
    for author in authors:
        try:
            payloads = works_by_author(
                author.openalex_id, since=author.last_work_at, max_results=max_results
            )
        except Exception:  # noqa: BLE001 — one bad author must not kill the run
            logger.exception(
                "author_ingest_failed", user_id=user.id, openalex_id=author.openalex_id
            )
            summary[author.author_name] = -1
            continue

        summary[author.author_name] = len(payloads)
        hits += len(payloads)
        newest = author.last_work_at
        for payload in payloads:
            paper = upsert_paper(payload)
            _, created = link_user_paper(user, paper, matched_keyword=author.author_name[:64])
            if created:
                linked += 1
            if payload.published_at and (newest is None or payload.published_at > newest):
                newest = payload.published_at
        author.last_work_at = newest

    db.session.commit()
    logger.info("author_ingest_done", user_id=user.id, hits=hits, linked=linked)
    return {"hits": hits, "linked": linked, "sources": summary}


def patent_sources(user: User | None = None) -> dict:
    """The deployment's usable patent sources, optionally narrowed to one
    user's preferences.

    "Usable" already means credentials are configured — `enabled_sources()`
    drops key-gated sources without them — and, when a user is given, that the
    admin has opted in and the user has not switched the source off.
    """
    available = {
        name: mod
        for name, mod in enabled_sources().items()
        if SOURCE_META.get(name, {}).get("category") == "patent"
    }
    if user is None:
        return available
    prefs = effective_source_prefs(user)
    return {name: mod for name, mod in available.items() if prefs.get(name)}


def patent_sources_available() -> bool:
    """Whether any patent source could run at all — keys present *and* the
    admin opt-in on.

    Read once by `patents.ingest_for_all_users` so a deployment that never
    configured EPO/PatentsView pays one registry lookup for the nightly
    schedule entry instead of a fan-out per user.
    """
    available = patent_sources()
    if not available:
        return False
    return any(
        admin_optin_key(name) is None or admin_opted_in(admin_optin_key(name)) for name in available
    )


def scrape_patents_for_user(user: User, *, max_results: int = 25) -> dict:
    """`scrape_for_user`, restricted to the patent sources.

    Deliberately a separate run rather than extra entries in the academic
    scan: patent sources are metered against a weekly budget, so an exhausted
    quota must not be able to mark a user's *academic* scan `partial` and
    drain the status line of meaning.

    Keyword handling is otherwise identical, including the English expansion —
    EPO's CQL and PatentsView's `_text_any` both search English text, so a
    user following "kalp yetmezliği" needs the same translation the academic
    sources get.
    """
    keyword_rows = list_user_keywords(user)
    if not keyword_rows:
        logger.info("patent_scrape_skip_no_keywords", user_id=user.id)
        return {"hits": 0, "linked": 0, "reason": "no_keywords"}

    sources = patent_sources(user)
    if not sources:
        logger.info("patent_scrape_skip_no_sources", user_id=user.id)
        return {"hits": 0, "linked": 0, "reason": "no_sources"}

    ensure_keyword_translations(keyword_rows, user=user)

    hits = 0
    linked = 0
    per_source: dict[str, int] = {}
    for name, source in sources.items():
        terms, alias = keyword_search_terms(keyword_rows, name)
        if not terms:
            continue
        try:
            payloads = source.search_for_keywords(terms, max_results=max_results)
        except Exception:  # noqa: BLE001 — one failing source must not kill the run
            logger.exception("patent_source_failed", source=name, user_id=user.id)
            per_source[name] = -1  # sentinel → apply_scan_result marks "partial"
            continue
        per_source[name] = len(payloads)
        hits += len(payloads)
        for payload in payloads:
            paper = upsert_paper(payload)
            _, created = link_user_paper(
                user, paper, matched_keyword=_match_keyword(paper.title, terms, alias)
            )
            if created:
                linked += 1
    logger.info("patent_scrape_done", user_id=user.id, hits=hits, linked=linked)
    return {"hits": hits, "linked": linked, "sources": per_source}


def search_patents_live(keywords: list[str], *, max_results: int = 20) -> tuple[list, dict]:
    """Query the patent sources directly and return payloads **without
    persisting them** — the prior-art search page (Faz 5.2).

    One-off prior-art queries must not pollute the `papers` table: a user
    checking twenty variations of an invention idea would otherwise leave
    hundreds of rows nobody's feed ever wanted, and EPO's fair-use terms are
    happier with a search that keeps nothing. Only the nightly keyword scan
    (`scrape_patents_for_user`) persists.

    Returns `(payloads, per_source)` where `per_source` uses the same -1
    sentinel as the scan paths so the page can say "EPO was rate limited"
    rather than silently showing fewer results.
    """
    terms = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not terms:
        return [], {}

    out: list = []
    per_source: dict[str, int] = {}
    for name, source in patent_sources().items():
        try:
            payloads = source.search_for_keywords(terms, max_results=max_results)
        except Exception:  # noqa: BLE001 — report the failure, keep the others
            logger.exception("patent_live_search_failed", source=name)
            per_source[name] = -1
            continue
        per_source[name] = len(payloads)
        out.extend(payloads)

    # Newest first: for prior art, a recent publication is the one most likely
    # to matter, and it also puts the "is this already claimed" answer on top.
    out.sort(key=lambda p: (p.published_at is not None, p.published_at), reverse=True)
    return out[:max_results], per_source


def _user_papers_query(user: User, view: str):
    """Shared filter builder for list/count. View vocabulary matches
    list_user_papers below."""
    q = UserPaper.query.filter_by(user_id=user.id)
    if view == "discover":
        q = q.filter(UserPaper.dismissed_at.is_(None))
    elif view == "favorites":
        q = q.filter(UserPaper.is_favorite.is_(True), UserPaper.dismissed_at.is_(None))
    elif view == "read_later":
        q = q.filter(UserPaper.read_later.is_(True), UserPaper.dismissed_at.is_(None))
    elif view == "dismissed":
        q = q.filter(UserPaper.dismissed_at.isnot(None))
    return q


def list_user_papers(
    user: User,
    *,
    limit: int = 50,
    view: str = "discover",
    q: str | None = None,
    kinds: tuple[str, ...] | None = None,
    quartiles: tuple[str, ...] | None = None,
) -> list[UserPaper]:
    """List a user's surfaced papers.

    Views:
        * "discover" — feed, hides dismissed rows
        * "favorites" — starred only
        * "dismissed" — hidden bin (recovery)
        * "all"      — everything, no filter

    `q` is an optional case-insensitive substring match against the
    paper's title, abstract, or matched keyword. Trimmed; empty == no
    filter.

    `kinds` restricts to specific `Paper.kind` values (e.g. `("video",
    "news")`). Default `None` means no filter — every existing caller is
    unaffected. This exists because the home page orders everything by
    `Paper.published_at DESC` across every kind, and the high-volume
    academic sources (arXiv et al. publish daily) always win the top slots;
    a user's own YouTube videos and RSS items get buried and never surface
    on the home page without a dedicated, kind-scoped query.

    `quartiles` restricts to papers whose journal carries one of the given
    SJR quartiles (e.g. `("Q1",)`). Papers with no seeded journal are excluded
    — "show me Q1 work" is a claim about the journal, and a paper we know
    nothing about does not satisfy it.
    """
    from sqlalchemy.orm import joinedload, selectinload

    query = (
        _user_papers_query(user, view)
        .join(Paper)
        .options(
            selectinload(UserPaper.notes),
            # `UserPaper.paper` is already `lazy="joined"` on the model, but
            # `Paper.video_summary` (uselist=False) is not — without this,
            # _paper_card.html's `r.paper.video_summary` check fires one
            # extra SELECT per row (N+1) across the feed's up-to-100 cards.
            joinedload(UserPaper.paper).joinedload(Paper.video_summary),
            # Same reasoning for the quartile badge (Faz 5.3): the card reads
            # `r.paper.journal.sjr_quartile`, which is one more SELECT per card
            # without this.
            joinedload(UserPaper.paper).joinedload(Paper.journal),
        )
    )
    if kinds:
        query = query.filter(Paper.kind.in_(kinds))
    if quartiles:
        # An inner join, not a filter on the outer join above: asking for Q1
        # means "papers in a Q1 journal", so papers with no journal row are
        # correctly excluded rather than silently kept.
        query = query.join(Journal, Paper.issn_l == Journal.issn_l).filter(
            Journal.sjr_quartile.in_(quartiles)
        )
    q = (q or "").strip()
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            db.or_(
                db.func.lower(Paper.title).like(like),
                db.func.lower(Paper.abstract).like(like),
                db.func.lower(UserPaper.matched_keyword).like(like),
                db.func.lower(db.cast(Paper.authors, db.String)).like(like),
            )
        )
    return query.order_by(desc(Paper.published_at), desc(UserPaper.created_at)).limit(limit).all()


def list_user_papers_in_window(
    user: User, start: datetime, end: datetime, *, limit: int | None = None
) -> list[UserPaper]:
    """Papers newly surfaced for `user` inside [start, end) — the digest's
    cost/scope guard: only summarise what's actually new in the window,
    never the whole feed. Mirrors the "discover" view (dismissed excluded),
    ordered by publish date like `list_user_papers`.

    `limit` bounds the query itself rather than leaving it to the caller to
    slice an unbounded result — a busy user with dozens of active sources can
    otherwise land hundreds of rows (each pulling its notes via
    `selectinload`) just to have `ai_service.generate_digest` throw all but
    `DIGEST_MAX_ITEMS` away. Defaults to twice that constant rather than the
    exact figure: the caller still does its own scoring/truncation, so the
    query leaves it a little room to pick the best items rather than
    whatever the DB happened to return first. Resolved from `ai_service`
    lazily (not at module import) to avoid a circular import between the two
    service modules.
    """
    from sqlalchemy.orm import selectinload

    if limit is None:
        from app.modules.scrape.ai_service import DIGEST_MAX_ITEMS

        limit = DIGEST_MAX_ITEMS * 2

    query = (
        _user_papers_query(user, "discover")
        .join(Paper)
        .options(selectinload(UserPaper.notes))
        .filter(UserPaper.created_at >= start, UserPaper.created_at < end)
    )
    return query.order_by(desc(Paper.published_at), desc(UserPaper.created_at)).limit(limit).all()


# ----------------------------------------------------------------------------
# RSS feed relevance — per-user LLM scoring + linking (Faz 2 Bölüm D)
#
# Different flow from scrape_for_user above: feeds are fetched once globally
# by app/tasks/feed_tasks.py:ingest_all (see app/modules/scrape/sources/
# rss_source.py), landing as Paper rows with kind="news" that no user has
# been linked to yet. This function does the per-user half — score the
# unlinked backlog against the user's interests and link what clears the
# threshold — so it belongs to a separate task (feeds.link_for_user) rather
# than the nightly scrape_for_user/run_for_user pair.
# ----------------------------------------------------------------------------

FEED_LINK_CANDIDATE_LIMIT = 50  # DB-side cap before the LLM's own windowing


def link_relevant_feed_items(
    user: User, *, threshold: int = 60, extra_candidates: list[Paper] | None = None
) -> dict:
    """Score recently-ingested `kind="news"` Papers this user isn't linked to
    yet, and link everything scoring >= threshold (matched_keyword = the
    LLM's pick of the most relevant interest term).

    Skips entirely (no LLM call) when the user has muted every enabled feed
    source via `UserSource` — opt-out is per-feed (each feed is its own
    source key, see sources/rss_source.py:FEEDS), so muting one feed still
    lets the others score normally.

    `extra_candidates` (Faz 3 Bölüm D) lets a caller fold in Papers from
    outside the curated-feed DB query — specifically the just-fetched output
    of `ingest_user_feeds(user)`. Custom feeds all share `Paper.source ==
    "user_feed"` across every user who's added one, so there's no reliable
    column to filter "this user's custom feeds" by in a query; the caller
    already knows exactly which Papers its own feeds just produced, so it's
    both simpler and more precise to pass them in directly than to guess from
    the DB. These bypass the curated-feed mute check (a user's own added feed
    isn't governed by the curated `UserSource` rows) but are still deduped
    against `already_linked`.
    """
    from app.modules.scrape.ai_service import score_feed_relevance
    from app.modules.scrape.sources.rss_source import FEEDS

    feed_keys = {f["key"] for f in FEEDS}
    prefs = list_user_source_prefs(user)
    active_feed_keys = {k for k in feed_keys if prefs.get(k, True)}

    already_linked = {
        row[0]
        for row in db.session.query(UserPaper.paper_id).filter(UserPaper.user_id == user.id).all()
    }

    candidates: list[Paper] = []
    seen_ids: set[int] = set()
    if active_feed_keys:
        query = Paper.query.filter(Paper.kind == "news", Paper.source.in_(active_feed_keys))
        if already_linked:
            query = query.filter(~Paper.id.in_(already_linked))
        for paper in (
            query.order_by(desc(Paper.published_at), desc(Paper.created_at))
            .limit(FEED_LINK_CANDIDATE_LIMIT)
            .all()
        ):
            candidates.append(paper)
            seen_ids.add(paper.id)

    for paper in extra_candidates or []:
        if paper.id in already_linked or paper.id in seen_ids:
            continue
        candidates.append(paper)
        seen_ids.add(paper.id)

    if not candidates:
        if not active_feed_keys and not extra_candidates:
            logger.info("feed_link_skip_all_muted", user_id=user.id)
            return {"scored": 0, "linked": 0, "reason": "news_muted"}
        return {"scored": 0, "linked": 0, "reason": "no_candidates"}

    scores = score_feed_relevance(user, candidates)
    by_id = {p.id: p for p in candidates}
    linked = 0
    for entry in scores:
        if entry["score"] < threshold:
            continue
        paper = by_id.get(entry["paper_id"])
        if paper is None:
            continue
        _, created = link_user_paper(user, paper, matched_keyword=entry.get("matched_keyword"))
        if created:
            linked += 1
    logger.info("feed_link_done", user_id=user.id, scored=len(scores), linked=linked)
    return {"scored": len(scores), "linked": linked}


# ----------------------------------------------------------------------------
# Custom user RSS feeds (Faz 3 Bölüm D)
# ----------------------------------------------------------------------------


def _normalize_feed_url(raw: str | None) -> str | None:
    """Trim, default to https:// when no scheme was given, and reject
    anything that doesn't parse into a real http(s) URL. Returns None on
    anything unusable."""
    url = (raw or "").strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url


def list_user_feeds(user: User) -> list[UserFeed]:
    return UserFeed.query.filter_by(user_id=user.id).order_by(desc(UserFeed.created_at)).all()


def get_user_feed(user: User, feed_id: int) -> UserFeed | None:
    """Fetch a UserFeed only if it belongs to this user — same ownership
    guard as get_note_for_user/get_user_paper."""
    return UserFeed.query.filter_by(id=feed_id, user_id=user.id).first()


def count_user_feeds(user: User) -> int:
    return UserFeed.query.filter_by(user_id=user.id).count()


def add_user_feed(
    user: User, url: str, label: str | None = None
) -> tuple[UserFeed | None, str | None]:
    """Validate + normalize the URL, fetch it once to confirm it actually
    yields entries, and auto-fill a label from the feed's own title when none
    was given. Returns (feed, None) on success or (None, error_message) on any
    validation/parse failure — never raises for a bad URL, that's an expected
    user input error, not a bug.

    Two limits guard the server here, both of which exist because this is the
    one place a user hands us an address we then fetch ourselves:

      * `MAX_USER_FEEDS` — each active feed is one HTTP request per nightly
        run, so an unbounded list is an unbounded per-user cost.
      * the SSRF guard — a feed pointing at `http://127.0.0.1:6379/` or the
        cloud metadata endpoint would otherwise be fetched from inside the
        network perimeter (see `net_guard.is_public_http_url`).

    Idempotent per (user, url): re-adding an existing URL just re-activates
    it (if it had been paused) instead of creating a duplicate row. Re-adding
    is therefore allowed even at the cap — it consumes no new slot.
    """
    normalized = _normalize_feed_url(url)
    if normalized is None:
        return None, _l("Please enter a valid feed URL (starting with http:// or https://).")

    existing = UserFeed.query.filter_by(user_id=user.id, url=normalized).first()
    if existing is not None:
        if not existing.active:
            existing.active = True
            # Re-activating forgets the validators — see the note below. While
            # the feed was paused it kept publishing, and a stale etag would
            # 304 straight past everything it published in the meantime.
            existing.etag = None
            existing.last_modified = None
            db.session.commit()
        return existing, None

    max_feeds = current_app.config.get("MAX_USER_FEEDS", 50)
    if count_user_feeds(user) >= max_feeds:
        logger.info("user_feed_cap_reached", user_id=user.id, cap=max_feeds)
        return None, FEED_CAP_MESSAGE

    allow_private = current_app.config.get("FEED_ALLOW_PRIVATE_HOSTS", False)
    ok, guard_error = is_public_http_url(normalized, allow_private=allow_private)
    if not ok:
        return None, guard_error

    parsed_feed = fetch_feed_conditional({"key": "user_feed", "url": normalized})
    if parsed_feed.status != "ok" or not parsed_feed.payloads:
        logger.warning(
            "user_feed_validate_failed",
            user_id=user.id,
            url=normalized,
            status=parsed_feed.status,
        )
        return None, _l("Could not read that feed — check the URL and try again.")

    clean_label = (label or "").strip()[:128] or None
    if not clean_label:
        clean_label = parsed_feed.title[:128] if parsed_feed.title else None

    # The validators from this fetch are deliberately NOT stored. This request
    # validates the URL; it does not ingest anything. Persisting its etag would
    # make the first nightly `ingest_user_feeds` answer 304 and skip the very
    # items that proved the feed was readable — the feed would look permanently
    # empty. One full fetch on the first run is the price of that correctness.
    row = UserFeed(
        user_id=user.id,
        url=normalized,
        label=clean_label,
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    logger.info("user_feed_added", user_id=user.id, feed_id=row.id, url=normalized)
    return row, None


def remove_user_feed(user: User, feed_id: int) -> bool:
    row = get_user_feed(user, feed_id)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def toggle_user_feed(user: User, feed_id: int) -> bool | None:
    """Flip a feed's active flag. Returns the new value, or None if the feed
    doesn't exist / isn't owned by this user (caller should 404).

    Resuming a paused feed clears its conditional-GET validators: the feed kept
    publishing while it was muted, and replaying a months-old etag would 304
    past the whole gap. Pausing leaves them alone — they are about to be
    cleared on resume anyway, and a paused feed is never fetched.
    """
    row = get_user_feed(user, feed_id)
    if row is None:
        return None
    row.active = not row.active
    if row.active:
        row.etag = None
        row.last_modified = None
    db.session.commit()
    return row.active


def ingest_user_feeds(user: User) -> tuple[dict, list[Paper]]:
    """Fetch this user's active custom RSS feeds and upsert new Papers
    (`source="user_feed"`, `kind="news"`) — the per-user counterpart of
    `feed_tasks.ingest_all` for the curated feeds. Called from
    `feed_tasks.link_for_user` right before `link_relevant_feed_items`, per
    user, never globally (a custom feed only exists because one user added it).

    `source="user_feed"` is shared across every user's custom feed (any URL)
    — the normal (source, external_id) dedup in `upsert_paper` means two
    users adding the identical URL land on the same Paper rows.

    Conditional GET: the row's stored `etag`/`last_modified` are sent back on
    every fetch and a 304 short-circuits that feed at zero parse/upsert cost,
    the same round-trip `ingest_user_channels` does. A 304 feed contributes
    nothing to `touched` on purpose — its items were already scored on the run
    that first ingested them, and re-scoring an unchanged feed every night is
    exactly the LLM spend this is here to avoid.

    Returns (summary_dict, touched_papers) — `touched_papers` is every Paper
    this run's fetch produced (new AND already-existing rows re-fetched from
    the still-active feed), which the caller feeds into
    `link_relevant_feed_items(..., extra_candidates=touched_papers)` so this
    user's own custom-feed content is eligible for relevance scoring even
    though it isn't part of the curated-feed DB query.
    """
    from app.modules.scrape.sources.rss_source import fetch_feed_conditional

    feeds = [f for f in list_user_feeds(user) if f.active]
    if not feeds:
        return {"hits": 0, "new": 0, "not_modified": 0}, []

    hits = 0
    new = 0
    not_modified = 0
    touched: list[Paper] = []
    for f in feeds:
        try:
            result = fetch_feed_conditional(
                {"key": "user_feed", "url": f.url},
                etag=f.etag,
                last_modified=f.last_modified,
            )
        except Exception:  # noqa: BLE001 — one broken custom feed must not block the others
            logger.exception("user_feed_fetch_failed", user_id=user.id, feed_id=f.id)
            continue

        if result.status == "not_modified":
            not_modified += 1
            continue
        if result.status != "ok":
            logger.warning(
                "user_feed_ingest_bad_status",
                user_id=user.id,
                feed_id=f.id,
                status=result.status,
            )
            continue

        hits += len(result.payloads)
        for payload in result.payloads:
            existed = (
                Paper.query.filter_by(
                    source=payload.source, external_id=payload.external_id
                ).first()
                is not None
            )
            paper = upsert_paper(payload)
            touched.append(paper)
            if not existed:
                new += 1

        # Validators are stored only after the upserts land. If the loop above
        # raises, this feed keeps its old etag and the next run re-fetches in
        # full — the opposite order would 304 past items we never persisted.
        f.etag = result.etag
        f.last_modified = result.last_modified
        db.session.commit()
    logger.info(
        "user_feeds_ingest_done",
        user_id=user.id,
        hits=hits,
        new=new,
        not_modified=not_modified,
    )
    return {"hits": hits, "new": new, "not_modified": not_modified}, touched


# ----------------------------------------------------------------------------
# Custom user YouTube channels — the twin of the custom-RSS-feed feature
# above, subscription-based the same way. CRUD first, `ingest_user_channels`
# (the Celery-driven ingestion, chaining transcript fetch + summarization)
# below it — see `app/tasks/channel_tasks.py` for the task that calls it.
# ----------------------------------------------------------------------------

#: Shown when a user tries to add channel number max_user_channels() + 1. Kept
#: as a module constant so the route, the template and the tests agree on it.
CHANNEL_CAP_MESSAGE = _l("Channel limit reached. Remove one before adding another.")


def max_user_channels() -> int:
    """The effective admin-set channel cap.

    Read from the `max_user_channels` system setting on every call, on
    purpose — an admin's change on `/settings/system` is live on the very
    next request, not just after a restart. Falls back to
    `MAX_USER_CHANNELS` (config/env) when no row exists yet.

    The setting lives in a JSON column an admin edits by hand, so it isn't
    trusted to already be an int: a garbage value (`"abc"`, `None`, a
    negative number) falls back to the config default / clamps to 0 rather
    than blowing up the add-channel flow.
    """
    from app.core.settings.service import get_system_setting

    fallback = current_app.config.get("MAX_USER_CHANNELS", 10)
    value = get_system_setting("max_user_channels", fallback)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(value, 0)


def list_user_channels(user: User) -> list[UserChannel]:
    return UserChannel.query.filter_by(user_id=user.id).order_by(desc(UserChannel.created_at)).all()


def get_user_channel(user: User, channel_pk: int) -> UserChannel | None:
    """Fetch a UserChannel only if it belongs to this user — same ownership
    guard as get_user_feed/get_note_for_user."""
    return UserChannel.query.filter_by(id=channel_pk, user_id=user.id).first()


def count_user_channels(user: User) -> int:
    return UserChannel.query.filter_by(user_id=user.id).count()


def add_user_channel(
    user: User, raw: str, label: str | None = None
) -> tuple[UserChannel | None, str | None]:
    """Resolve + register a YouTube channel subscription. Returns
    (channel, None) on success or (None, error_message) on any
    validation/resolution failure — never raises for bad user input.

    Step order mirrors add_user_feed:
      1. blank input is rejected before any resolution attempt
      2. resolve_channel(raw) does the actual work — SSRF guard, page/handle
         lookup and a live validating fetch of the channel's own RSS feed.
         Its error message is already plain, user-facing English, so it's
         returned as-is rather than repeating those checks here.
      3. re-adding an already-subscribed channel just reactivates it
         (if paused) instead of creating a duplicate row, and — like
         add_user_feed — consumes no cap slot to do so.
      4. only a genuinely new subscription is checked against the cap.
    """
    from app.modules.scrape.sources.youtube_channel_source import resolve_channel

    raw = (raw or "").strip()
    if not raw:
        return None, _l("Please enter a YouTube channel URL or @handle.")

    resolved, error = resolve_channel(raw)
    if resolved is None:
        return None, error

    existing = UserChannel.query.filter_by(
        user_id=user.id, channel_id=resolved["channel_id"]
    ).first()
    if existing is not None:
        if not existing.active:
            existing.active = True
            db.session.commit()
        return existing, None

    cap = max_user_channels()
    if count_user_channels(user) >= cap:
        logger.info("user_channel_cap_reached", user_id=user.id, cap=cap)
        return None, CHANNEL_CAP_MESSAGE

    clean_label = (label or "").strip()[:200] or None
    if not clean_label:
        clean_label = (resolved.get("title") or "")[:200] or None

    row = UserChannel(
        user_id=user.id,
        channel_id=resolved["channel_id"],
        title=clean_label,
        url=resolved["url"],
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    logger.info("user_channel_added", user_id=user.id, channel_pk=row.id, channel_id=row.channel_id)
    return row, None


def remove_user_channel(user: User, channel_pk: int) -> bool:
    row = get_user_channel(user, channel_pk)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def toggle_user_channel(user: User, channel_pk: int) -> bool | None:
    """Flip a channel's active flag. Returns the new value, or None if the
    channel doesn't exist / isn't owned by this user (caller should 404)."""
    row = get_user_channel(user, channel_pk)
    if row is None:
        return None
    row.active = not row.active
    db.session.commit()
    return row.active


def ingest_user_channels(user: User) -> tuple[dict, list[int]]:
    """Fetch this user's active YouTube channel subscriptions and upsert new
    Papers (`source="youtube_channel"`, `kind="video"`) — the channel
    counterpart of `ingest_user_feeds` above, called from
    `channel_tasks.ingest_for_user`.

    Unlike the RSS feed path (`UserFeed`/`ingest_user_feeds`), which stores an
    etag/last_modified on the row but never sends it back on the next fetch,
    this one round-trips them: `fetch_channel_videos` gets the row's stored
    values and a `not_modified` response short-circuits that channel at zero
    parsing/upsert cost.

    In practice that branch is currently dead for YouTube specifically:
    `feeds/videos.xml` answers with `Cache-Control: max-age=900` and **no
    ETag and no Last-Modified**, so both columns stay NULL and every run
    re-downloads the ~15 entries. Measured, not assumed — don't "fix" the
    empty etag column, there is nothing to store. The plumbing is kept
    because it costs nothing, works the moment YouTube starts sending
    validators, and is the pattern the feed path still needs to adopt.

    Returns `(summary, new_paper_ids)`:
      * `summary` maps `channel_id -> count` for that channel this run — `-1`
        is the same "this one errored" sentinel `scrape_for_user`/
        `feeds.ingest_all` use, so `apply_scan_result` reads it unchanged.
      * `new_paper_ids` is every Paper id newly *created* this run (for
        `channel_tasks.ingest_for_user` to hand to `summarize_video`).
        "Newly created" is decided the same way `ingest_user_feeds` decides
        it: a pre-upsert existence check on `(source, external_id)`, not
        `link_user_paper`'s `created` flag. The two diverge when a video's
        Paper row already exists (another user subscribes to the same
        channel) but is new to *this* user's library — that case must not
        queue a second summarization job for a video someone already
        summarized.

    One broken channel must never abort the loop — per-channel try/except,
    same isolation contract as `ingest_user_feeds`'s per-feed try/except.
    """
    from app.modules.scrape.sources.youtube_channel_source import fetch_channel_videos

    channels = [c for c in list_user_channels(user) if c.active]
    if not channels:
        return {}, []

    summary: dict[str, int] = {}
    new_paper_ids: list[int] = []
    for ch in channels:
        try:
            payloads, status, etag, last_modified, _title = fetch_channel_videos(
                ch.channel_id, etag=ch.etag, last_modified=ch.last_modified
            )
        except Exception:  # noqa: BLE001 — one broken channel must not block the others
            logger.exception(
                "user_channel_ingest_failed", user_id=user.id, channel_id=ch.channel_id
            )
            summary[ch.channel_id] = -1
            continue

        if status == "not_modified":
            summary[ch.channel_id] = 0
            continue
        if status != "ok":
            logger.warning(
                "user_channel_ingest_bad_status",
                user_id=user.id,
                channel_id=ch.channel_id,
                status=status,
            )
            summary[ch.channel_id] = -1
            continue

        ch.etag = etag
        ch.last_modified = last_modified

        newest_published_at = ch.last_video_at
        for payload in payloads:
            existed = (
                Paper.query.filter_by(
                    source=payload.source, external_id=payload.external_id
                ).first()
                is not None
            )
            paper = upsert_paper(payload)
            link_user_paper(user, paper, matched_keyword=None)
            if not existed:
                new_paper_ids.append(paper.id)
            if payload.published_at and (
                newest_published_at is None or payload.published_at > newest_published_at
            ):
                newest_published_at = payload.published_at

        if newest_published_at is not None:
            ch.last_video_at = newest_published_at

        db.session.commit()
        summary[ch.channel_id] = len(payloads)

    logger.info(
        "user_channels_ingest_done", user_id=user.id, summary=summary, new=len(new_paper_ids)
    )
    return summary, new_paper_ids


def count_user_papers(user: User, view: str = "discover") -> int:
    """COUNT(*) for the same view filters list_user_papers uses. Library /
    Discover tab badges use this instead of materialising 500 rows just to
    take len() of them."""
    from sqlalchemy import func

    q = _user_papers_query(user, view).with_entities(func.count(UserPaper.id))
    return q.scalar() or 0


def search_user_papers_query(
    user: User,
    *,
    q: str | None = None,
    source: str | None = None,
    date_from=None,
    date_to=None,
    has_notes: bool = False,
    quartile: str | None = None,
):
    """Return a SQLAlchemy query for the user's papers matching the filters.

    Returns a query (not a list) so the caller can `.paginate()`. Scoped to the
    user and hides dismissed papers — search is over the live library. All
    filters are ANDed; each is skipped when empty.

    `quartile` ("Q1".."Q4") restricts to papers whose journal carries that SJR
    quartile; papers with no seeded journal are excluded, because "Q1 only" is
    a claim about the journal that an unknown one does not satisfy.
    """
    from sqlalchemy.orm import joinedload, selectinload

    query = (
        UserPaper.query.filter(UserPaper.user_id == user.id, UserPaper.dismissed_at.is_(None))
        .join(Paper)
        .options(
            selectinload(UserPaper.notes),
            # Same N+1 guard as list_user_papers — this feeds
            # scrape/_paper_card.html via library/search.html too.
            joinedload(UserPaper.paper).joinedload(Paper.video_summary),
            joinedload(UserPaper.paper).joinedload(Paper.journal),
        )
    )

    q = (q or "").strip()
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            db.or_(
                db.func.lower(Paper.title).like(like),
                db.func.lower(Paper.abstract).like(like),
                db.func.lower(UserPaper.matched_keyword).like(like),
                db.func.lower(db.cast(Paper.authors, db.String)).like(like),
            )
        )
    if source:
        query = query.filter(Paper.source == source)
    if quartile:
        query = query.join(Journal, Paper.issn_l == Journal.issn_l).filter(
            Journal.sjr_quartile == quartile
        )
    if date_from is not None:
        query = query.filter(Paper.published_at >= date_from)
    if date_to is not None:
        query = query.filter(Paper.published_at <= date_to)
    if has_notes:
        # Only papers the user has written at least one note on.
        query = query.filter(UserPaper.notes.any())

    return query.order_by(desc(Paper.published_at), desc(UserPaper.created_at))


def distinct_user_sources(user: User) -> list[str]:
    """The sources present in this user's library — powers the filter dropdown
    so it never offers a source the user has no papers from."""
    rows = (
        db.session.query(Paper.source)
        .join(UserPaper, UserPaper.paper_id == Paper.id)
        .filter(UserPaper.user_id == user.id, UserPaper.dismissed_at.is_(None))
        .distinct()
        .order_by(Paper.source)
        .all()
    )
    return [s for (s,) in rows]


def to_bibtex(paper: Paper) -> str:
    """Render a paper as a BibTeX entry. arXiv → @misc, anything else → @article.

    Keep it minimal and safe (no template engine) — researchers paste this
    into their .bib so the format must round-trip BibTeX parsers cleanly.
    """
    # Stable cite key: <firstAuthorLastname><year><externalId-suffix>
    year = paper.published_at.year if paper.published_at else "n.d."
    first_author = (paper.authors or ["unknown"])[0]
    last = first_author.split()[-1].lower() if first_author else "unknown"
    ext_tail = (paper.external_id or "").split("/")[-1].replace(".", "")[:8]
    cite_key = f"{last}{year}{ext_tail}"

    entry_type = "misc" if paper.source == "arxiv" else "article"
    authors = " and ".join(paper.authors or [])
    fields = [
        f"  title     = {{{paper.title}}},",
        f"  author    = {{{authors}}}," if authors else None,
        f"  year      = {{{year}}}," if year != "n.d." else None,
        f"  url       = {{{paper.url}}}," if paper.url else None,
        f"  eprint    = {{{paper.external_id}}}," if paper.source == "arxiv" else None,
        "  archivePrefix = {arXiv}," if paper.source == "arxiv" else None,
        f"  abstract  = {{{(paper.abstract or '').strip()}}}," if paper.abstract else None,
    ]
    body = "\n".join(f for f in fields if f)
    return f"@{entry_type}{{{cite_key},\n{body}\n}}\n"


def papers_to_bibtex(papers: list[UserPaper]) -> str:
    """Concatenate BibTeX entries for a set of user_papers — the bulk export
    a researcher drops into Zotero/Mendeley/EndNote."""
    return "\n".join(to_bibtex(up.paper) for up in papers)


def papers_to_csv(papers: list[UserPaper]) -> str:
    """CSV export of a user's papers. Uses the csv module so titles/abstracts
    with commas, quotes, or newlines are quoted correctly (naive f-string
    joining would corrupt the file on the first comma in a title)."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "source",
            "external_id",
            "title",
            "authors",
            "published_at",
            "url",
            "pdf_url",
            "categories",
            "is_favorite",
            "read_later",
            "matched_keyword",
        ]
    )
    for up in papers:
        p = up.paper
        writer.writerow(
            [
                p.source,
                p.external_id,
                p.title,
                "; ".join(p.authors or []),
                p.published_at.date().isoformat() if p.published_at else "",
                p.url or "",
                p.pdf_url or "",
                "; ".join(p.categories or []),
                up.is_favorite,
                up.read_later,
                up.matched_keyword or "",
            ]
        )
    return buf.getvalue()


def count_all_notes(user: User) -> int:
    from sqlalchemy import func

    return (
        db.session.query(func.count(PaperNote.id))
        .join(UserPaper, PaperNote.user_paper_id == UserPaper.id)
        .filter(UserPaper.user_id == user.id)
        .scalar()
        or 0
    )


def get_user_paper(user: User, user_paper_id: int) -> UserPaper | None:
    """Fetch a UserPaper that belongs to this user. Returns None on miss or
    ownership mismatch — callers should treat both the same way."""
    return UserPaper.query.filter_by(id=user_paper_id, user_id=user.id).first()


# ----------------------------------------------------------------------------
# Per-user paper state — favorites, dismiss, mark seen
# ----------------------------------------------------------------------------


def set_favorite(link: UserPaper, value: bool) -> bool:
    """Set the favorite flag to an explicit value. Idempotent — this is the
    primitive the API wants, since a retried request must not flip state
    back. Returns the new value."""
    link.is_favorite = bool(value)
    db.session.commit()
    return link.is_favorite


def toggle_favorite(link: UserPaper) -> bool:
    """Flip the favorite flag. Returns the new value so the caller can pick
    the right flash/UI state without re-querying."""
    return set_favorite(link, not link.is_favorite)


def set_read_later(link: UserPaper, value: bool) -> bool:
    """Set the read-later flag explicitly. See set_favorite."""
    link.read_later = bool(value)
    db.session.commit()
    return link.read_later


def toggle_read_later(link: UserPaper) -> bool:
    """Flip the read later (bookmark) flag."""
    return set_read_later(link, not link.read_later)


def set_dismissed(link: UserPaper, dismissed: bool) -> None:
    link.dismissed_at = datetime.now(UTC) if dismissed else None
    db.session.commit()


def mark_seen(link: UserPaper) -> None:
    if link.seen_at is None:
        link.seen_at = datetime.now(UTC)
        db.session.commit()


# ----------------------------------------------------------------------------
# Notes
# ----------------------------------------------------------------------------

ALLOWED_NOTE_TAGS = {"deney", "soru", "sonuç", "okuma", None, ""}


def add_note(link: UserPaper, body: str, tag: str | None = None) -> PaperNote | None:
    """Create a note on a UserPaper. Empty/whitespace-only bodies are
    rejected (returns None) — that's the only validation the service does."""
    body = (body or "").strip()
    if not body:
        return None
    tag = (tag or "").strip().lower() or None
    if tag not in ALLOWED_NOTE_TAGS:
        tag = None
    note = PaperNote(user_paper_id=link.id, body=body, tag=tag)
    db.session.add(note)
    db.session.commit()
    return note


def delete_note(note: PaperNote) -> None:
    db.session.delete(note)
    db.session.commit()


def edit_note(note: PaperNote, body: str, tag: str | None = None) -> bool:
    """In-place note update. Empty body is a no-op (returns False) — match
    the add_note contract so callers can re-use validation logic."""
    body = (body or "").strip()
    if not body:
        return False
    tag = (tag or "").strip().lower() or None
    if tag not in ALLOWED_NOTE_TAGS:
        tag = None
    note.body = body
    note.tag = tag
    db.session.commit()
    return True


def get_note_for_user(user: User, note_id: int) -> PaperNote | None:
    """Fetch a note only if its parent UserPaper belongs to this user.

    Guards against /papers/notes/<other-user-id> drive-by deletes — we
    enforce ownership at the service layer so every caller is safe.
    """
    return (
        PaperNote.query.join(UserPaper, PaperNote.user_paper_id == UserPaper.id)
        .filter(PaperNote.id == note_id, UserPaper.user_id == user.id)
        .first()
    )


def list_all_notes(user: User, *, limit: int = 100) -> list[PaperNote]:
    """Every note this user has ever written, newest first. Used by the
    Library "Notes" tab to show notes across papers in one stream."""
    return (
        PaperNote.query.join(UserPaper, PaperNote.user_paper_id == UserPaper.id)
        .filter(UserPaper.user_id == user.id)
        .order_by(desc(PaperNote.created_at))
        .limit(limit)
        .all()
    )


# ----------------------------------------------------------------------------
# Timeline — merged activity stream for the Library page
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineEvent:
    """A single row on the Library timeline. Multiple source tables
    (user_papers, paper_notes, audit_logs, user_keywords) are folded into
    this one shape so the template just renders a list."""

    when: datetime
    kind: str  # see KIND_* constants below
    title: str
    detail: str | None = None
    badge: str | None = None  # short status pill (e.g. "+12 new")
    link_url: str | None = None
    tag: str | None = None  # for note events


KIND_SCRAPE = "scrape_run"
KIND_PATENT_SCAN = "patent_run"
KIND_AUTHOR_SCAN = "author_run"
KIND_FAVORITED = "favorited"
KIND_NOTE_ADDED = "note_added"
KIND_DISMISSED = "dismissed"
KIND_KEYWORD_ADDED = "keyword_added"


def build_timeline(user: User, *, limit: int = 40) -> list[TimelineEvent]:
    """Build a merged activity stream of recent events for this user.

    Sources, in priority order:
      * scan_runs     — scrape runs (manual + nightly fan-out)
      * paper_notes   — every note the user wrote
      * user_papers   — favorited (uses updated_at as a proxy when is_favorite)
                        + dismissed (dismissed_at) + newly surfaced (created_at)
      * user_keywords — when the user added a new research interest

    Each source query is capped by `limit` independently so a quiet week
    of one source doesn't crowd out another.
    """
    from urllib.parse import quote

    from app.modules.academic.models import Keyword, UserKeyword

    events: list[TimelineEvent] = []

    # ---- Scrape runs ----
    # Read from scan_runs, not audit_logs. The audit path only ever recorded
    # manual runs: it also looked for a "scrape.auto_run" action that nothing
    # wrote, so nightly runs never appeared here at all.
    runs = (
        ScanRun.query.filter(
            ScanRun.user_id == user.id,
            ScanRun.kind == "scrape",
            ScanRun.status.in_(("ok", "partial", "error")),
        )
        .order_by(desc(ScanRun.started_at))
        .limit(limit)
        .all()
    )
    for r in runs:
        events.append(
            TimelineEvent(
                when=r.finished_at or r.started_at,
                kind=KIND_SCRAPE,
                title="Manuel tarama" if r.trigger == "manual" else "Otomatik tarama",
                detail=None,
                badge=f"+{r.new_items}" if r.new_items else None,
            )
        )

    # ---- Patent scans ----
    # Their own event kind rather than folding into the scrape runs above: a
    # patent run is a separate ScanRun (see `scrape_patents_for_user`), and
    # showing both as "tarama" would make two rows look like a duplicate.
    patent_runs = (
        ScanRun.query.filter(
            ScanRun.user_id == user.id,
            ScanRun.kind == "patents",
            ScanRun.status.in_(("ok", "partial", "error")),
        )
        .order_by(desc(ScanRun.started_at))
        .limit(limit)
        .all()
    )
    for r in patent_runs:
        events.append(
            TimelineEvent(
                when=r.finished_at or r.started_at,
                kind=KIND_PATENT_SCAN,
                title="Manuel patent taraması" if r.trigger == "manual" else "Patent taraması",
                detail=None,
                badge=f"+{r.new_items}" if r.new_items else None,
            )
        )

    # ---- Author scans ----
    author_runs = (
        ScanRun.query.filter(
            ScanRun.user_id == user.id,
            ScanRun.kind == "authors",
            ScanRun.status.in_(("ok", "partial", "error")),
        )
        .order_by(desc(ScanRun.started_at))
        .limit(limit)
        .all()
    )
    for r in author_runs:
        events.append(
            TimelineEvent(
                when=r.finished_at or r.started_at,
                kind=KIND_AUTHOR_SCAN,
                title="Takip edilen yazarlar",
                detail=None,
                badge=f"+{r.new_items}" if r.new_items else None,
            )
        )

    # ---- Note events ----
    notes = list_all_notes(user, limit=limit)
    for n in notes:
        link = n.user_paper
        # Body preview — first ~70 chars on one line
        preview = (n.body or "").replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:78] + "…"
        events.append(
            TimelineEvent(
                when=n.created_at,
                kind=KIND_NOTE_ADDED,
                title=link.paper.title,
                detail=preview,
                tag=n.tag,
                link_url=f"/papers/{link.id}",
            )
        )

    # ---- Favorites + dismissals ----
    fav_rows = (
        UserPaper.query.filter(UserPaper.user_id == user.id, UserPaper.is_favorite.is_(True))
        .order_by(desc(UserPaper.updated_at))
        .limit(limit)
        .all()
    )
    for r in fav_rows:
        # updated_at is None when the row was inserted as favorite at scrape
        # time (it never had a separate flip). Fall back to created_at.
        when = r.updated_at or r.created_at
        events.append(
            TimelineEvent(
                when=when,
                kind=KIND_FAVORITED,
                title=r.paper.title,
                detail=r.matched_keyword,
                link_url=f"/papers/{r.id}",
            )
        )

    dis_rows = (
        UserPaper.query.filter(UserPaper.user_id == user.id, UserPaper.dismissed_at.isnot(None))
        .order_by(desc(UserPaper.dismissed_at))
        .limit(limit)
        .all()
    )
    for r in dis_rows:
        events.append(
            TimelineEvent(
                when=r.dismissed_at,
                kind=KIND_DISMISSED,
                title=r.paper.title,
                detail=r.matched_keyword,
                link_url=f"/papers/{r.id}",
            )
        )

    # ---- Keyword adds ----
    kw_links = (
        UserKeyword.query.filter_by(user_id=user.id)
        .join(Keyword)
        .order_by(desc(UserKeyword.created_at))
        .limit(limit)
        .all()
    )
    for ul in kw_links:
        events.append(
            TimelineEvent(
                when=ul.created_at,
                kind=KIND_KEYWORD_ADDED,
                title=ul.keyword.value,
                detail=None,
                link_url=f"/papers/?q={quote(ul.keyword.value)}",
            )
        )

    events.sort(key=lambda e: e.when, reverse=True)
    return events[:limit]
