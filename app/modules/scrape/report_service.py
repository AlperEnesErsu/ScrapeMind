"""Retrospective reports (Faz 6) — creation, quota, and the topic map/reduce
pipeline.

A separate domain file from `service.py` on purpose (see that file's line
count) even though it leans on several of its exports (`upsert_paper`,
`list_author_groups`/`list_group_members`/`GROUP_NOT_FOUND_MESSAGE` for the
"author_group" kind, and the `Report`/`Journal`/`Paper`/`AuthorGroup`/
`UserAuthor` models) plus `academic.service.list_user_keywords` for the
group report's keyword-overlap stat. Two moving parts live here:

  * `create_report` — validates a request and writes a `pending` `Report`
    row. Never raises (`service.follow_author`'s shape: `(row, None)` or
    `(None, message)`), and enforces `MAX_REPORTS_PER_DAY` because a report
    run costs one `works_in_range` call, up to five `aggregate_works` calls,
    and a batch of LLM calls (map + reduce) — a burst of these is real money.

  * `run_report` — the generator itself. `kind="topic"` aggregates one
    OpenAlex query; `kind="author_group"` fans out per member instead (one
    `openalex_source.works_by_author` call each — see `_run_author_group`),
    isolating a single member's failure the same way `service.scrape_for_user`
    isolates a single source's.

Statistics (`Report.stats`) are computed with **no LLM call** — every number
comes from `openalex_source.aggregate_works`/`works_in_range`/`works_by_author`
or from this module's own arithmetic over their output. The LLM only ever
narrates on top of that backbone (`ai_service.summarize_report_chunk`/
`synthesize_report`), and its absence or failure degrades the report to
`status="partial"` rather than losing it — `stats` alone is still something
to show a user.

`ai_service.synthesize_report` has one fixed prompt/schema shared by every
report kind (it does not know about "topic" vs "author_group" — extending it
is `ai_service.py`'s call, not this module's, see the header note on file
ownership). For "author_group", `_author_group_sections` reshapes its
generic `tldr`/`for_your_keywords` output into this kind's own `sections`
shape (`tldr`/`members`/`shared_topics`/`overlap_with_you`/
`coauthorship_notes`) — the per-member breakdown itself comes from data this
module already computed (the map-phase chunks + the deterministic `stats`
backbone), not from a second LLM schema `synthesize_report` was never asked
to produce.

`SourceThrottledError` is caught at the narrowest possible point (each
`aggregate_works`/`works_in_range` call, via `_safe_aggregate`/`_fetch_works`)
rather than around the whole run, specifically so a mid-run rate limit keeps
whatever dimensions were already fetched instead of discarding them — the
report still finishes with partial `stats`, not none at all.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from flask_babel import gettext as _
from flask_babel import lazy_gettext as _l

from app.extensions import db
from app.modules.academic.service import list_user_keywords
from app.modules.scrape import ai_service
from app.modules.scrape.models import AuthorGroup, Journal, Paper, Report, UserAuthor
from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.service import (
    GROUP_NOT_FOUND_MESSAGE,
    list_author_groups,
    list_group_members,
    upsert_paper,
)
from app.modules.scrape.sources import openalex_source as oa

logger = structlog.get_logger()

# Cost guards. A report run is not cheap (one multi-page OpenAlex fetch, up
# to five aggregate calls, and a batch of LLM calls) — this cap is per user,
# not per kind, because the expense is the same regardless of which kind was
# requested.
MAX_REPORTS_PER_DAY = 3

# One `ai_service.summarize_report_chunk` call per this many items — kept in
# lockstep with `ai_service.MAX_TOKENS_REPORT_MAP`'s sizing assumption.
REPORT_CHUNK_ITEMS = 25

REPORT_MIN_YEARS = 1
REPORT_MAX_YEARS = 10

#: `Report.kind` values accepted at creation time.
VALID_KINDS = frozenset({"topic", "author_group"})

#: Of `VALID_KINDS`, the ones `run_report` actually knows how to execute.
#: Kept as its own set (rather than just checking `VALID_KINDS`) so a future
#: third kind can be accepted by `create_report` — sit in `pending` — before
#: its generator exists, same staged rollout "author_group" itself had.
_RUNNABLE_KINDS = frozenset({"topic", "author_group"})

#: Rows kept per dimension in `stats["top_*"]` (topic) and in the group-level
#: `stats["shared_*"]`/`stats["coauthorship"]`/`stats["keyword_overlap"]`
#: dimensions (author_group) — enough for a briefing card, not the whole
#: distribution (`aggregate_works` can return dozens of groups for a broad
#: topic; a large group can share dozens of topics/venues).
_STATS_TOP_N = 10

#: Rows kept per *member* ranking (`top_venues`/`top_topics`/`top_cited` in
#: `stats["members"][name]`) — smaller than `_STATS_TOP_N` because this is
#: one card per person, not the group's headline numbers.
_MEMBER_TOP_N = 5

#: Cap on `openalex_source.works_by_author` results fetched per group member
#: per report run. `MAX_GROUP_MEMBERS` (20) members at this cap is the actual
#: worst case for one run's OpenAlex spend — smaller than
#: `openalex_source.REPORT_MAX_WORKS` (400) because that constant bounds one
#: *pooled* topic query, not 20 separate per-author ones.
REPORT_MEMBER_MAX_WORKS = 50

_GROUP_BY_YEAR = "publication_year"
_GROUP_BY_AUTHORS = "authorships.author.id"
_GROUP_BY_VENUES = "primary_location.source.id"
_GROUP_BY_TOPICS = "primary_topic.id"
_GROUP_BY_OA = "open_access.is_oa"

REPORT_KIND_INVALID_MESSAGE = _l("Unsupported report type.")
REPORT_KEYWORDS_REQUIRED_MESSAGE = _l("Please provide at least one keyword for a topic report.")
REPORT_DAILY_CAP_MESSAGE = _l("Daily report limit reached. Please try again tomorrow.")
#: "Group not found" is deliberately reused from `service.py` (not a new
#: message) for the not-owned case too — same information-hiding reasoning
#: as `get_report`/`delete_report` scoping their query to `user_id=user.id`
#: instead of a separate "not yours" message that would confirm the id exists.
REPORT_AUTHOR_GROUP_EMPTY_MESSAGE = _l(
    "This group has no members yet. Add at least one author before generating a report."
)


# ----------------------------------------------------------------------------
# create_report
# ----------------------------------------------------------------------------


def _clean_keywords(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [kw.strip() for kw in raw if isinstance(kw, str) and kw.strip()]


def _topic_query_and_range(params: dict) -> tuple[str, int, int]:
    """`(query, since_year, until_year)` for a "topic" report's params.

    `years` is a *span*, not a literal year — `until_year` is always "this
    year" at generation time (not whatever year the report was first
    created), so re-running an old report naturally extends its window
    forward instead of staying pinned to the past.
    """
    keywords = _clean_keywords(params.get("keywords"))
    try:
        years = int(params.get("years"))
    except (TypeError, ValueError):
        years = REPORT_MIN_YEARS
    years = max(REPORT_MIN_YEARS, min(years, REPORT_MAX_YEARS))
    until_year = datetime.now(UTC).year
    since_year = until_year - years + 1
    query = " OR ".join(keywords)
    return query, since_year, until_year


def _report_title(params: dict) -> str:
    keywords = _clean_keywords(params.get("keywords"))
    _query, since_year, until_year = _topic_query_and_range(params)
    label = ", ".join(keywords) or "?"
    title = f"{label} ({since_year}–{until_year})"
    return title[:255]


def _owned_group_for_report(user, group_id: Any) -> AuthorGroup | None:
    """Resolve `group_id` to an `AuthorGroup` this `user` owns, or `None`.

    Goes through `service.list_author_groups` — the module's public,
    already-ownership-scoped export — rather than `service._owned_author_group`,
    which is private to `service.py` (leading underscore) and not this
    module's to reach past. `MAX_AUTHOR_GROUPS` (10) keeps this a short scan,
    not a query worth adding a new `service` export for.
    """
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        return None
    for group in list_author_groups(user):
        if group.id == group_id:
            return group
    return None


def _author_group_report_title(group_name: str, years: int) -> str:
    """Same "span, not a literal year" reasoning as `_report_title` — see
    that function's docstring for why `until_year` is always "this year" at
    generation time rather than pinned to when the report was first created.
    """
    until_year = datetime.now(UTC).year
    since_year = until_year - years + 1
    title = f"{group_name} ({since_year}–{until_year})"
    return title[:255]


def create_report(user, kind: str, params: dict) -> tuple[Report | None, str | None]:
    """Validate a report request and write a `pending` row.

    Never raises — returns `(row, None)` on success or `(None, message)` on
    a rejected request, same shape as `service.follow_author`. Validates
    (in order): `kind` is recognised, kind-specific params (a keyword for
    "topic"; an owned, non-empty group for "author_group"), the requested
    year span is within `[REPORT_MIN_YEARS, REPORT_MAX_YEARS]`, and the user
    hasn't hit `MAX_REPORTS_PER_DAY` in the trailing 24 hours (counted across
    every kind — the expense a report costs doesn't depend on which kind it
    is).

    For "author_group", ownership of `params["group_id"]` is a real security
    boundary (not just a friendlier error): a user must not be able to spend
    their daily quota generating a report over someone else's followed
    authors. Membership is checked too — a group with no members yet has
    nothing for `run_report` to fetch.
    """
    kind = (kind or "").strip()
    if kind not in VALID_KINDS:
        return None, REPORT_KIND_INVALID_MESSAGE

    params = dict(params or {})
    group: AuthorGroup | None = None

    if kind == "topic":
        keywords = _clean_keywords(params.get("keywords"))
        if not keywords:
            return None, REPORT_KEYWORDS_REQUIRED_MESSAGE
        params["keywords"] = keywords

        try:
            years = int(params.get("years"))
        except (TypeError, ValueError):
            years = None
        if years is None or not (REPORT_MIN_YEARS <= years <= REPORT_MAX_YEARS):
            return None, _(
                "Year range must be between %(min)s and %(max)s years.",
                min=REPORT_MIN_YEARS,
                max=REPORT_MAX_YEARS,
            )
        params["years"] = years
    else:  # "author_group" — the only other member of VALID_KINDS
        group = _owned_group_for_report(user, params.get("group_id"))
        if group is None:
            return None, GROUP_NOT_FOUND_MESSAGE
        if not list_group_members(group):
            return None, REPORT_AUTHOR_GROUP_EMPTY_MESSAGE
        params["group_id"] = group.id

        try:
            years = int(params.get("years"))
        except (TypeError, ValueError):
            years = None
        if years is None or not (REPORT_MIN_YEARS <= years <= REPORT_MAX_YEARS):
            return None, _(
                "Year range must be between %(min)s and %(max)s years.",
                min=REPORT_MIN_YEARS,
                max=REPORT_MAX_YEARS,
            )
        params["years"] = years

    since = datetime.now(UTC) - timedelta(hours=24)
    recent_count = Report.query.filter(
        Report.user_id == user.id, Report.created_at >= since
    ).count()
    if recent_count >= MAX_REPORTS_PER_DAY:
        logger.info("report_daily_cap_reached", user_id=user.id, count=recent_count)
        return None, REPORT_DAILY_CAP_MESSAGE

    title = (
        _report_title(params)
        if kind == "topic"
        else _author_group_report_title(group.name, params["years"])
    )
    report = Report(
        user_id=user.id,
        kind=kind,
        title=title,
        params=params,
        status="pending",
    )
    db.session.add(report)
    db.session.commit()
    logger.info("report_created", user_id=user.id, report_id=report.id, kind=kind)
    return report, None


# ----------------------------------------------------------------------------
# run_report — statistics backbone (no LLM)
# ----------------------------------------------------------------------------


def _safe_aggregate(
    query: str, since_year: int, until_year: int, group_by: str, throttled: list[bool]
) -> list[dict]:
    """One `aggregate_works` call that degrades to `[]` instead of raising —
    both when the shared OpenAlex budget is already known to be exhausted
    (`throttled[0]` already `True`, so this doesn't even try) and when this
    very call is the one that hits the limit.

    `throttled` is a one-element list used as a mutable out-param: every
    stats dimension (and `_fetch_works` afterwards) shares one flag so a
    rate limit hit on, say, the third aggregation stops the remaining ones
    from wasting a call on a budget that's already gone, without needing
    `nonlocal` across a nest of closures.
    """
    if throttled[0]:
        return []
    try:
        return oa.aggregate_works(
            query, since_year=since_year, until_year=until_year, group_by=group_by
        )
    except SourceThrottledError:
        throttled[0] = True
        logger.warning("report_stats_throttled", group_by=group_by)
        return []


def _year_series(year_groups: list[dict]) -> list[dict]:
    rows = []
    for g in year_groups:
        try:
            year = int(g.get("key"))
        except (TypeError, ValueError):
            continue
        rows.append({"year": year, "count": g.get("count") or 0})
    rows.sort(key=lambda r: r["year"])
    return rows


#: What OpenAlex's `open_access.is_oa` group-by calls "open access". It keys
#: the boolean as "1"/"0" and puts "true"/"false" in `key_display_name`, so
#: `aggregate_works` surfaces the words under `name` and the digits under
#: `key`. Matching only `key == "true"` therefore matched nothing and every
#: report came out at 0% OA — caught against the live API, not by a test,
#: because the test's fixture had assumed the friendlier shape.
_OA_TRUE = frozenset({"true", "1"})


def _compute_oa_share(oa_groups: list[dict]) -> float | None:
    """Fraction of works that are open access, from the
    `open_access.is_oa` group-by. Both the display name and the raw key are
    checked (see `_OA_TRUE`).

    `None` when there is nothing to divide by (empty groups, or a throttled
    call that returned `[]`) — a report with no OA figure at all reads
    better than a fabricated 0%.
    """
    if not oa_groups:
        return None
    total = sum(g.get("count") or 0 for g in oa_groups)
    if not total:
        return None
    oa_count = sum(
        (g.get("count") or 0)
        for g in oa_groups
        if str(g.get("name") or "").strip().lower() in _OA_TRUE
        or str(g.get("key") or "").strip().lower() in _OA_TRUE
    )
    return round(oa_count / total, 4)


def _growing_topics(
    query: str, since_year: int, until_year: int, throttled: list[bool], *, top_n: int = 5
) -> list[dict]:
    """Topics whose count grew the most from the first half of the range to
    the second half — two extra `aggregate_works` calls, still no LLM.

    Needs at least two distinct years to mean anything (a 1-year report has
    no "before" half to compare against), so a single-year span returns `[]`
    without spending a call.
    """
    if until_year <= since_year:
        return []
    mid = since_year + (until_year - since_year) // 2
    early = _safe_aggregate(query, since_year, mid, _GROUP_BY_TOPICS, throttled)
    late = _safe_aggregate(query, mid + 1, until_year, _GROUP_BY_TOPICS, throttled)

    early_counts = {g["key"]: (g.get("count") or 0) for g in early if g.get("key")}
    growth = []
    for g in late:
        key = g.get("key")
        if not key:
            continue
        late_count = g.get("count") or 0
        delta = late_count - early_counts.get(key, 0)
        if delta <= 0:
            continue
        growth.append(
            {"key": key, "name": g.get("name") or key, "count": late_count, "growth": delta}
        )
    growth.sort(key=lambda g: g["growth"], reverse=True)
    return growth[:top_n]


def _build_stats(query: str, since_year: int, until_year: int, throttled: list[bool]) -> dict:
    """The LLM-free numeric backbone: year-by-year counts, top authors/
    venues/topics, OA share, and which topics grew fastest. `journal_
    quartiles` is intentionally left empty here — it needs the *persisted*
    `Paper` rows (see `_journal_quartile_distribution`), which only exist
    after `run_report`'s upsert step, so it is filled in there.
    """
    year_groups = _safe_aggregate(query, since_year, until_year, _GROUP_BY_YEAR, throttled)
    year_series = _year_series(year_groups)
    total = sum(r["count"] for r in year_series)

    top_authors = _safe_aggregate(query, since_year, until_year, _GROUP_BY_AUTHORS, throttled)
    top_venues = _safe_aggregate(query, since_year, until_year, _GROUP_BY_VENUES, throttled)
    top_topics = _safe_aggregate(query, since_year, until_year, _GROUP_BY_TOPICS, throttled)
    oa_groups = _safe_aggregate(query, since_year, until_year, _GROUP_BY_OA, throttled)
    growing_topics = _growing_topics(query, since_year, until_year, throttled)

    return {
        "query": query,
        "since_year": since_year,
        "until_year": until_year,
        "total": total,
        "year_series": year_series,
        "top_authors": top_authors[:_STATS_TOP_N],
        "top_venues": top_venues[:_STATS_TOP_N],
        "top_topics": top_topics[:_STATS_TOP_N],
        "oa_share": _compute_oa_share(oa_groups),
        "growing_topics": growing_topics,
        "journal_quartiles": {},
    }


def _journal_quartile_distribution(papers: list[Paper]) -> dict[str, int]:
    """Scimago quartile distribution for this report's persisted papers.

    `Paper.journal` (see `models.py`) is a `viewonly` relationship joining on
    `issn_l`, but walking it per paper would be an N+1 query across up to
    `openalex_source.REPORT_MAX_WORKS` rows — this does the same join as one
    batched `Journal.issn_l.in_(...)` query instead.

    A paper with no ISSN, or an ISSN the `journals` table hasn't been seeded
    for (see `docs/PHASE5.md` — Scimago seeding is manual), is counted under
    "unknown" rather than dropped, so the distribution always sums to
    `len(papers)`.
    """
    issns = {p.issn_l for p in papers if p.issn_l}
    quartile_by_issn: dict[str, str | None] = {}
    if issns:
        rows = Journal.query.filter(Journal.issn_l.in_(issns)).all()
        quartile_by_issn = {j.issn_l: j.sjr_quartile for j in rows}

    counts: dict[str, int] = defaultdict(int)
    for p in papers:
        quartile = quartile_by_issn.get(p.issn_l) if p.issn_l else None
        counts[quartile or "unknown"] += 1
    return dict(counts)


def _fetch_works(query: str, since_year: int, until_year: int, throttled: list[bool]) -> list:
    """The report's underlying item set — most-cited works in range, capped
    at `openalex_source.REPORT_MAX_WORKS`. Shares `throttled` with the stats
    calls above: if the budget is already gone, this returns `[]` instead of
    spending (and failing) one more call."""
    if throttled[0]:
        return []
    try:
        return oa.works_in_range(
            query, since_year=since_year, until_year=until_year, max_results=oa.REPORT_MAX_WORKS
        )
    except SourceThrottledError:
        throttled[0] = True
        logger.warning("report_works_throttled")
        return []


# ----------------------------------------------------------------------------
# run_report — author_group statistics backbone (no LLM)
# ----------------------------------------------------------------------------


def _fetch_member_works(
    member: UserAuthor, since: datetime, max_results: int, throttled: list[bool]
) -> list | None:
    """One member's `openalex_source.works_by_author` results for the
    report window, or `None` on failure.

    Two distinct failure shapes, both isolated so one member never sinks the
    whole run (`service.scrape_for_user`'s per-source isolation, applied per
    member instead of per source):

      * `SourceThrottledError` shares `throttled` with every other OpenAlex
        call this report makes — same flag `_safe_aggregate`/`_fetch_works`
        use for "topic" — so once the budget is gone, every *remaining*
        member is skipped without spending a call, not just this one.
      * Anything else (a bad/stale author id, a one-off network error) is
        this member's own problem: logged and reported as `None`, the same
        `-1`-sentinel idea `service.scrape_for_user`/`ingest_user_authors`
        use for "this one thing broke, the rest didn't".

    Returns `None` on either failure, otherwise the (possibly empty) payload
    list.
    """
    if throttled[0]:
        return None
    try:
        return oa.works_by_author(member.openalex_id, since=since, max_results=max_results)
    except SourceThrottledError:
        throttled[0] = True
        logger.warning("report_member_throttled", author_name=member.author_name)
        return None
    except Exception:  # noqa: BLE001 — one bad member must not kill the report
        logger.exception("report_member_fetch_failed", author_name=member.author_name)
        return None


def _member_year_series(payloads: list) -> list[dict]:
    counts: dict[int, int] = defaultdict(int)
    for p in payloads:
        year = getattr(p.published_at, "year", None) if p.published_at is not None else None
        if year is not None:
            counts[year] += 1
    return [{"year": year, "count": count} for year, count in sorted(counts.items())]


def _member_topic_counts(payloads: list, *, top_n: int = _MEMBER_TOP_N) -> list[dict]:
    """Top `categories` (OpenAlex concept/topic display names —
    `openalex_source._to_payload` is what fills this field) across one
    member's fetched works. LLM-free and payload-only, unlike `_build_stats`'
    `top_topics` (a server-side `aggregate_works` group-by) — a per-member
    breakdown over one already-fetched `works_by_author` page isn't worth a
    second network round trip."""
    counts: dict[str, int] = defaultdict(int)
    for p in payloads:
        for topic in p.categories or []:
            if topic:
                counts[topic] += 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [{"name": name, "count": count} for name, count in ranked[:top_n]]


def _member_top_venues(
    payloads: list, issn_title_map: dict[str, str], *, top_n: int = _MEMBER_TOP_N
) -> list[dict]:
    """Top venues by resolved journal title (see `_issn_title_map`). A work
    whose ISSN the `journals` table hasn't been seeded for (manual Scimago
    seed, see `docs/PHASE5.md`) is simply not counted here — unlike
    `_journal_quartile_distribution`'s "unknown" bucket, a ranked top-N list
    has nothing useful to say about an unresolved venue, so it is dropped
    rather than inflating an "Unknown" entry.
    """
    counts: dict[str, int] = defaultdict(int)
    for p in payloads:
        title = issn_title_map.get(p.issn_l) if p.issn_l else None
        if title:
            counts[title] += 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [{"name": name, "count": count} for name, count in ranked[:top_n]]


def _member_top_cited(payloads: list, *, top_n: int = _MEMBER_TOP_N) -> list[dict]:
    ranked = sorted(
        (p for p in payloads if p.cited_by_count is not None),
        key=lambda p: p.cited_by_count,
        reverse=True,
    )
    return [
        {
            "title": p.title,
            "year": getattr(p.published_at, "year", None),
            "cited_by_count": p.cited_by_count,
        }
        for p in ranked[:top_n]
    ]


def _issn_title_map(member_payloads: dict[str, list | None]) -> dict[str, str]:
    """One batched `Journal.issn_l.in_(...)` lookup across every member's
    fetched works — same N+1-avoidance reasoning as
    `_journal_quartile_distribution`, just keyed by title instead of
    quartile and sourced from `PaperPayload.issn_l` directly (cheaper than
    waiting for the upsert step's persisted `Paper` rows, and this map is
    needed before that step runs)."""
    issns = {
        p.issn_l for payloads in member_payloads.values() if payloads for p in payloads if p.issn_l
    }
    if not issns:
        return {}
    rows = Journal.query.filter(Journal.issn_l.in_(issns)).all()
    return {j.issn_l: j.title for j in rows if j.title}


def _shared_dimension(
    member_payloads: dict[str, list | None], extractor, *, top_n: int = _STATS_TOP_N
) -> list[dict]:
    """Values of `extractor(payload)` (topic names, or resolved venue
    titles) that show up in at least two *different* members' fetched work
    lists — the group-level "what do these people have in common" signal,
    computed the same LLM-free way as everything else in this section.
    """
    value_to_members: dict[str, set[str]] = defaultdict(set)
    for name, payloads in member_payloads.items():
        if not payloads:
            continue
        for p in payloads:
            for value in extractor(p):
                if value:
                    value_to_members[value].add(name)
    shared = [
        {"name": value, "member_count": len(names)}
        for value, names in value_to_members.items()
        if len(names) >= 2
    ]
    shared.sort(key=lambda s: s["member_count"], reverse=True)
    return shared[:top_n]


def _coauthorship_links(member_payloads: dict[str, list | None]) -> list[dict]:
    """Pairs of group members who co-authored the same work.

    Detected by two *different* members' own `works_by_author` results
    sharing the same paper identity (DOI when present, else `(source,
    external_id)`) — if both A and B's individually-fetched lists contain
    the same paper, OpenAlex credits both of them on it. That is a direct
    collaboration signal and deliberately not string-matching display names
    inside `PaperPayload.authors`, which has no stable identifier and is
    exactly the kind of "two different J. Smiths" ambiguity
    `service.follow_author`'s docstring already flags for this data.
    """
    paper_to_members: dict[tuple, dict] = {}
    for name, payloads in member_payloads.items():
        if not payloads:
            continue
        for p in payloads:
            key = ("doi", p.doi.lower()) if p.doi else ("id", p.source, p.external_id)
            entry = paper_to_members.setdefault(key, {"title": p.title, "members": set()})
            entry["members"].add(name)

    links: dict[tuple[str, str], dict] = {}
    for entry in paper_to_members.values():
        names = sorted(entry["members"])
        if len(names) < 2:
            continue
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                pair = (names[i], names[j])
                link = links.setdefault(
                    pair, {"members": list(pair), "count": 0, "example_title": entry["title"]}
                )
                link["count"] += 1
    return sorted(links.values(), key=lambda link: link["count"], reverse=True)


def _keyword_overlap(member_payloads: dict[str, list | None], keywords: list) -> dict:
    """How much of this group's recent output already touches the user's
    own followed keywords (`academic.service.list_user_keywords`) — the
    "actual payoff" of an author-group report per the feature's design:
    simple, case-insensitive substring matching against title+abstract, no
    LLM involved (a keyword hit is either there in the text or it isn't).

    `Keyword.search_terms` (see `academic.models.Keyword`) already folds in
    the English canonical form + variants, which is what makes a Turkish
    keyword like "kalp yetmezliği" match an English-language OpenAlex
    abstract at all — same reasoning `service.scrape_for_user` relies on for
    the nightly scan.
    """
    terms = sorted({t.lower() for kw in keywords for t in kw.search_terms if t})
    if not terms:
        return {"matched_keywords": [], "matches": [], "match_count": 0}

    matched_terms: set[str] = set()
    matches: list[dict] = []
    seen: set[tuple] = set()
    for name, payloads in member_payloads.items():
        if not payloads:
            continue
        for p in payloads:
            haystack = f"{p.title or ''} {p.abstract or ''}".lower()
            hits = [t for t in terms if t in haystack]
            if not hits:
                continue
            matched_terms.update(hits)
            key = (p.source, p.external_id)
            if key in seen:
                continue
            seen.add(key)
            matches.append({"title": p.title, "member": name, "matched_terms": hits[:5]})
    matches.sort(key=lambda m: m["title"] or "")
    return {
        "matched_keywords": sorted(matched_terms),
        "matches": matches[:_STATS_TOP_N],
        "match_count": len(matches),
    }


def _build_author_group_stats(
    group: AuthorGroup | None,
    members: list[UserAuthor],
    since_year: int,
    until_year: int,
    member_payloads: dict[str, list | None],
    keywords: list,
) -> dict:
    """The author_group counterpart to `_build_stats` — everything here is
    arithmetic over `member_payloads` (each resolved member's own
    `works_by_author` results), no LLM call.

    `member_payloads` only has an entry per member with a resolved
    `openalex_id`; members without one never had a fetch attempted (see
    `run_report`/`_run_author_group`) and are reported separately, under
    `unresolved_members`, so a resolved identity going quiet is never
    confused with one that was never resolvable in the first place. A value
    of `None` in `member_payloads` means that member's fetch itself failed
    (`_fetch_member_works`) — kept as its own per-member entry (`None`, not
    omitted) so the failure is visible rather than silently dropping the
    member from the report.
    """
    resolved_names = set(member_payloads)
    unresolved = [m.author_name for m in members if not m.openalex_id]

    issn_title_map = _issn_title_map(member_payloads)

    per_member: dict[str, dict | None] = {}
    for member in members:
        if member.author_name not in resolved_names:
            continue
        payloads = member_payloads[member.author_name]
        if payloads is None:
            per_member[member.author_name] = None
            continue
        per_member[member.author_name] = {
            "openalex_id": member.openalex_id,
            "total": len(payloads),
            "year_series": _member_year_series(payloads),
            "top_venues": _member_top_venues(payloads, issn_title_map),
            "top_topics": _member_topic_counts(payloads),
            "top_cited": _member_top_cited(payloads),
        }

    all_payloads = [p for payloads in member_payloads.values() if payloads for p in payloads]

    return {
        "group_id": group.id if group is not None else None,
        "group_name": group.name if group is not None else "",
        "since_year": since_year,
        "until_year": until_year,
        "member_count": len(members),
        "resolved_member_count": len(resolved_names),
        "unresolved_members": unresolved,
        "members": per_member,
        "total": len(all_payloads),
        "shared_topics": _shared_dimension(member_payloads, lambda p: p.categories or []),
        "shared_venues": _shared_dimension(
            member_payloads,
            lambda p: [issn_title_map[p.issn_l]] if p.issn_l in issn_title_map else [],
        ),
        "coauthorship": _coauthorship_links(member_payloads),
        "keyword_overlap": _keyword_overlap(member_payloads, keywords),
        "journal_quartiles": {},
    }


# ----------------------------------------------------------------------------
# run_report — map/reduce narrative (LLM)
# ----------------------------------------------------------------------------


def _map_phase(payloads: list, *, user) -> tuple[list[dict], bool]:
    """Group `payloads` by publication year, chunk each year's group into
    `REPORT_CHUNK_ITEMS`-sized pieces, and summarize each chunk via
    `ai_service.summarize_report_chunk`.

    Returns `(chunk_summaries, any_chunk_failed)`. A chunk that came back
    `None` (AI disabled, or the LLM call/parse failed even after its own
    repair retry) is simply omitted from `chunk_summaries` — it must not
    kill the whole report — and `any_chunk_failed` tells the caller that at
    least one chunk was lost, which is what downgrades the run to
    `status="partial"`.

    `ref` is assigned 1-based *within each chunk* (see
    `ai_service.summarize_report_chunk`'s docstring — each call is
    self-contained, refs are not global across chunks).
    """
    if not payloads:
        return [], False

    by_year: dict[int | None, list] = defaultdict(list)
    for p in payloads:
        year = getattr(p.published_at, "year", None) if p.published_at is not None else None
        by_year[year].append(p)

    summaries: list[dict] = []
    any_failed = False
    for year in sorted(by_year, key=lambda y: (y is None, y)):
        group = by_year[year]
        label = str(year) if year is not None else "yıl bilinmiyor"
        for start in range(0, len(group), REPORT_CHUNK_ITEMS):
            chunk = group[start : start + REPORT_CHUNK_ITEMS]
            items = [
                {
                    "title": item.title,
                    "abstract": item.abstract,
                    "year": year,
                    "cited_by_count": item.cited_by_count,
                    "ref": idx,
                }
                for idx, item in enumerate(chunk, start=1)
            ]
            context = f"{label} yılı, {len(chunk)} çalışma"
            summary = ai_service.summarize_report_chunk(items, context=context, user=user)
            if summary is None:
                any_failed = True
                continue
            summaries.append(summary)
    return summaries, any_failed


def _map_phase_by_member(
    member_payloads: dict[str, list | None], *, user
) -> tuple[list[dict], dict[str, list[dict]], bool]:
    """Same job as `_map_phase`, chunked by group member instead of by
    publication year — an author-group report's narrative axis is "what has
    each member been doing lately", not "what happened in a given year".

    Returns `(flat_chunk_summaries, per_member_chunk_summaries, any_failed)`:
    `flat_chunk_summaries` feeds `ai_service.synthesize_report` exactly like
    "topic"'s does (that function only ever pools themes/notable/methods
    across the whole run — it has no concept of "member"), while
    `per_member_chunk_summaries` is kept aside so `_author_group_sections`
    can build each member's own `focus`/`recent_highlights` without a second
    LLM pass. A member with no fetched payloads (unresolved identity, or a
    failed fetch — see `_fetch_member_works`) contributes nothing to either
    and is not counted as a map failure — there was nothing to summarize.
    """
    flat: list[dict] = []
    per_member: dict[str, list[dict]] = {}
    any_failed = False
    for name, payloads in member_payloads.items():
        if not payloads:
            continue
        member_summaries: list[dict] = []
        for start in range(0, len(payloads), REPORT_CHUNK_ITEMS):
            chunk = payloads[start : start + REPORT_CHUNK_ITEMS]
            items = [
                {
                    "title": item.title,
                    "abstract": item.abstract,
                    "year": getattr(item.published_at, "year", None),
                    "cited_by_count": item.cited_by_count,
                    "ref": idx,
                }
                for idx, item in enumerate(chunk, start=1)
            ]
            context = f"{name}, {len(chunk)} çalışma"
            summary = ai_service.summarize_report_chunk(items, context=context, user=user)
            if summary is None:
                any_failed = True
                continue
            member_summaries.append(summary)
            flat.append(summary)
        if member_summaries:
            per_member[name] = member_summaries
    return flat, per_member, any_failed


def _coauthorship_notes_text(links: list[dict]) -> str:
    """Plain-text rendering of `stats["coauthorship"]` for the
    `coauthorship_notes` section field — deterministic, not LLM-authored
    (there is nothing to interpret in "these two people co-authored N
    things"), but only ever assembled from inside `_author_group_sections`,
    which itself returns `{}` whenever the LLM reduce step failed — see that
    function's docstring for why a deterministic piece still rides inside
    the all-or-nothing `sections` payload instead of living in `stats`."""
    if not links:
        return ""
    parts = [f"{link['members'][0]} & {link['members'][1]} ({link['count']})" for link in links[:5]]
    return "; ".join(parts)


def _author_group_sections(
    synth: dict | None, per_member_chunks: dict[str, list[dict]], stats: dict
) -> dict:
    """Merge the LLM's dossier narrative with this module's own numbers into
    `Report.sections`: `{"tldr", "members", "shared_topics",
    "overlap_with_you", "coauthorship_notes"}`.

    The split is deliberate. `ai_service.synthesize_author_group_report`
    writes the parts that need judgement — what a member works on
    (`focus`), which papers are worth calling out, where the group overlaps
    the user's own keywords. Everything countable stays here: a member's
    venues and topics are ranked facts, and asking a model to restate them
    only adds a way for them to come back wrong.

    Members are keyed by name: the LLM is told the exact names and its
    entries are matched back to them, so a member the model skipped still
    appears (with its counted venues/topics and an empty `focus`) rather
    than vanishing from the dossier. A member whose fetch failed is left out
    entirely — there is nothing to narrate and `stats` already records the
    failure.

    `shared_topics` and `coauthorship_notes` fall back to the computed
    values when the model omits them, because those two are derivable
    without it; `overlap_with_you` has no fallback, since "how this relates
    to your work" is exactly the part only the model can phrase.

    Returns `{}` when `synth` is `None` (AI disabled, or the reduce call or
    parse failed) — all-or-nothing, the same contract the "topic" report's
    `sections` has. `run_report` still shows `stats` on its own either way.
    """
    if not synth:
        return {}

    by_name = {m.get("name"): m for m in (synth.get("members") or []) if isinstance(m, dict)}

    members_section: list[dict] = []
    for name, member_stats in (stats.get("members") or {}).items():
        if member_stats is None:
            continue  # this member's fetch failed — nothing to narrate
        written = by_name.get(name) or {}
        members_section.append(
            {
                "name": name,
                "focus": (written.get("focus") or "")[:600],
                "recent_highlights": (written.get("recent_highlights") or [])[:5],
                "venues": [v["name"] for v in member_stats.get("top_venues", [])],
                "topics": [t["name"] for t in member_stats.get("top_topics", [])],
            }
        )

    computed_shared = [t["name"] for t in stats.get("shared_topics") or []]
    return {
        "tldr": synth.get("tldr") or "",
        "members": members_section,
        "shared_topics": synth.get("shared_topics") or computed_shared,
        "overlap_with_you": synth.get("overlap_with_you") or "",
        "coauthorship_notes": (
            synth.get("coauthorship_notes")
            or _coauthorship_notes_text(stats.get("coauthorship") or [])
        ),
    }


def _run_author_group(
    report: Report, params: dict
) -> tuple[dict, list[Paper], list[dict], dict[str, list[dict]], bool, bool, list[bool]]:
    """Fetch + assemble everything `run_report`'s "author_group" branch
    needs, mirroring the "topic" branch's `_build_stats`/`_fetch_works`/
    `_map_phase` trio but fanned out per member.

    Group ownership was already checked once, at `create_report` time — a
    report re-run only needs to resolve the id again, not re-authorize it
    (same "idempotent rerun" reasoning `run_report`'s docstring gives for
    "topic"). If the group was deleted since, this degrades to a
    zero-member run rather than raising: stale `report.params` is not the
    same thing as a bug in this function.

    Returns `(stats, papers, chunk_summaries, per_member_chunks, map_failed,
    extra_partial, throttled)`. `extra_partial` is this branch's analogue of
    "topic"'s `map_failed`/`throttled[0]` union: `True` when at least one
    *resolved* member's own fetch failed (`_fetch_member_works` returned
    `None`), which — unlike an unresolved identity being skipped — is a
    real gap in the report and must downgrade `run_report`'s final status to
    `"partial"`.
    """
    group = _owned_group_for_report(report.user, params.get("group_id"))
    members = list_group_members(group) if group is not None else []

    try:
        years = int(params.get("years"))
    except (TypeError, ValueError):
        years = REPORT_MIN_YEARS
    until_year = datetime.now(UTC).year
    since_year = until_year - years + 1
    since = datetime(since_year, 1, 1, tzinfo=UTC)

    throttled = [False]
    member_payloads: dict[str, list | None] = {}
    extra_partial = False
    for member in members:
        if not member.openalex_id:
            continue  # unresolved identity — surfaced via stats["unresolved_members"]
        payloads = _fetch_member_works(member, since, REPORT_MEMBER_MAX_WORKS, throttled)
        member_payloads[member.author_name] = payloads
        if payloads is None:
            extra_partial = True

    keywords = list_user_keywords(report.user)
    stats = _build_author_group_stats(
        group, members, since_year, until_year, member_payloads, keywords
    )

    papers = [
        upsert_paper(payload)
        for payloads in member_payloads.values()
        if payloads
        for payload in payloads
    ]
    stats["journal_quartiles"] = _journal_quartile_distribution(papers)

    chunk_summaries, per_member_chunks, map_failed = _map_phase_by_member(
        member_payloads, user=report.user
    )
    return stats, papers, chunk_summaries, per_member_chunks, map_failed, extra_partial, throttled


# ----------------------------------------------------------------------------
# run_report — orchestration
# ----------------------------------------------------------------------------


def run_report(report: Report) -> dict:
    """Execute a `pending`/`running` report end to end and persist the
    result onto `report`. Both `VALID_KINDS` are runnable today: "topic"
    pools one OpenAlex query, "author_group" fans out one `works_by_author`
    call per group member (see `_run_author_group`). `_RUNNABLE_KINDS` is
    kept as a separate set from `VALID_KINDS` anyway (see its docstring) so
    a future third kind can be accepted by `create_report` — sit in
    `pending` — before its generator exists here, same staged rollout
    "author_group" itself had; any such kind is rejected immediately with
    `status="error"` rather than silently producing an empty report.

    Idempotent: re-running the same `report` row re-fetches, re-upserts
    (`service.upsert_paper` dedupes on DOI/`(source, external_id)`), and
    overwrites `stats`/`sections`/`status` on the same row rather than
    creating anything new.

    Failure handling:
      * `SourceThrottledError` from OpenAlex is caught at the narrowest
        possible point (`_safe_aggregate`/`_fetch_works` for "topic",
        `_fetch_member_works` for "author_group"), never here — by the time
        control reaches this function's `try`, a rate limit has already been
        folded into `throttled[0]` and the run continues with whatever
        partial `stats`/items it managed to collect. The result is
        `status="partial"`, not a raised exception. "author_group" has one
        more partial trigger with no "topic" equivalent: `extra_partial`,
        set when a *resolved* member's own fetch failed outright (not a
        shared-budget throttle, just that one member — see
        `_run_author_group`'s docstring).
      * Any other exception is unexpected: `status="error"`,
        `error=type(exc).__name__` (never the message — see `Report`'s
        docstring on why), and the exception is re-raised so Celery's retry
        machinery sees it, same contract as `service.record_scan_run`.
    """
    if report.kind not in _RUNNABLE_KINDS:
        report.status = "error"
        report.error = "UnsupportedReportKind"
        db.session.commit()
        logger.error("report_kind_not_runnable", report_id=report.id, kind=report.kind)
        return {"status": report.status, "error": report.error}

    report.status = "running"
    report.started_at = datetime.now(UTC)
    db.session.commit()

    try:
        params = dict(report.params or {})
        extra_partial = False

        if report.kind == "topic":
            query, since_year, until_year = _topic_query_and_range(params)

            throttled = [False]
            stats = _build_stats(query, since_year, until_year, throttled)
            payloads = _fetch_works(query, since_year, until_year, throttled)

            papers = [upsert_paper(payload) for payload in payloads]
            stats["journal_quartiles"] = _journal_quartile_distribution(papers)

            chunk_summaries, map_failed = _map_phase(payloads, user=report.user)
            sections = ai_service.synthesize_report(chunk_summaries, stats, user=report.user)
            sections_failed = sections is None
        else:  # "author_group" — the only other member of _RUNNABLE_KINDS
            (
                stats,
                papers,
                chunk_summaries,
                per_member_chunks,
                map_failed,
                extra_partial,
                throttled,
            ) = _run_author_group(report, params)

            reduced = ai_service.synthesize_author_group_report(
                per_member_chunks, stats, user=report.user
            )
            sections = _author_group_sections(reduced, per_member_chunks, stats)
            sections_failed = reduced is None
    except Exception as exc:
        report.status = "error"
        report.error = type(exc).__name__
        report.finished_at = datetime.now(UTC)
        db.session.commit()
        logger.exception("report_run_failed", report_id=report.id, kind=report.kind)
        raise

    report.stats = stats
    report.sections = sections or {}
    report.item_count = len(papers)
    report.model_version = (
        ai_service._model_label(report.user) if ai_service.is_ai_enabled(report.user) else None
    )
    report.finished_at = datetime.now(UTC)
    report.status = (
        "partial" if (throttled[0] or map_failed or extra_partial or sections_failed) else "ok"
    )
    db.session.commit()
    logger.info(
        "report_run_finished",
        report_id=report.id,
        status=report.status,
        item_count=report.item_count,
    )
    return {"status": report.status, "item_count": report.item_count}


# ----------------------------------------------------------------------------
# Listing / lookup / deletion
# ----------------------------------------------------------------------------


def list_user_reports(user, *, kind: str | None = None, limit: int = 20) -> list[Report]:
    q = Report.query.filter_by(user_id=user.id)
    if kind:
        q = q.filter_by(kind=kind)
    return q.order_by(Report.created_at.desc()).limit(limit).all()


def get_report(user, report_id: int) -> Report | None:
    return Report.query.filter_by(id=report_id, user_id=user.id).first()


def delete_report(user, report_id: int) -> bool:
    """Delete a report. Returns whether a row was actually removed (false
    for someone else's report or an id that doesn't exist) — same shape as
    `service.unfollow_author`."""
    report = get_report(user, report_id)
    if report is None:
        return False
    db.session.delete(report)
    db.session.commit()
    return True
