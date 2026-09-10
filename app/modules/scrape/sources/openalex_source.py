"""OpenAlex adapter.

Uses the public works-search endpoint:
  https://api.openalex.org/works

No API key exists or is needed — OpenAlex is fully open. What it does gate on
is *politeness*: requests that identify a contact address (via `mailto=` and,
per OpenAlex's own recommendation, a matching User-Agent) are routed into the
"polite pool", which gets faster and more consistent response times. Without
OPENALEX_MAILTO set, requests still work, they just share the common pool
with every anonymous caller on the internet. Set OPENALEX_MAILTO to a real
contact address for this deployment to get the better pool.

The `search` parameter supports boolean OR natively (like PubMed, unlike
Semantic Scholar), so `search_for_keywords` issues a single combined request
rather than looping per keyword.

Same contract as the other adapters: pure I/O, returns PaperPayload rows,
never raises for a single bad record (skips it), raises requests exceptions
upward so the service layer can isolate a failing source.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

import requests
import structlog

from app.modules.scrape.doi import normalize_doi
from app.modules.scrape.ratelimit import SourceThrottledError, openalex_slot
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "openalex"

_API_URL = "https://api.openalex.org/works"
_TIMEOUT = 20  # seconds

# One Session per worker process, not one per call. Every function in this
# module hits the same host (api.openalex.org), and a nightly run can call
# `search`/`works_by_author`/`fetch_by_doi` hundreds of times back to back
# (once per user's scan, once per followed author, once per Scopus hydration)
# — a fresh TCP+TLS handshake for each of those is pure overhead. `Session`'s
# default `HTTPAdapter` already caps its pool (10 connections), so this
# cannot grow unbounded; it is not resized here.
#
# One assumption worth stating: `Session` is not documented as thread-safe.
# Every task that reaches this module runs on the `scrape` queue, which is a
# prefork pool — one process, one session, no sharing. The threads pool
# (`-P threads`, dev compose) only consumes `io`. Routing an OpenAlex-touching
# task to `io` would break that assumption silently, so don't, or give the
# threaded path its own session.
#
# This is a deliberate, narrow exception to "adapters use module-level
# `requests`" (CLAUDE.md rule 7) for OpenAlex specifically — see the
# docstring note in this module's tests for the resulting monkeypatch-target
# change (`oa._session.get`, not `oa.requests.get`).
_session = requests.Session()

# A malformed/adversarial abstract_inverted_index (untrusted remote JSON)
# shouldn't be able to produce an unbounded string via a huge position value
# or a pathological number of words.
_MAX_ABSTRACT_CHARS = 20_000

# OpenAlex caps a page at 200 results.
_MAX_PER_PAGE = 200

# PubMed caps its MeSH terms at 8 (see pubmed_source._parse_article); match
# that so no single source dominates a paper's category list.
_MAX_CATEGORIES = 8


def _mailto() -> str:
    return os.getenv("OPENALEX_MAILTO", "")


def _params(query: str, max_results: int) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "search": query,
        "per-page": min(max_results, _MAX_PER_PAGE),
    }
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto
    return params


def _headers() -> dict[str, str]:
    mailto = _mailto()
    return {"User-Agent": f"ScrapeMind (mailto:{mailto})"} if mailto else {}


def _parse_date(item: dict) -> datetime | None:
    """publication_date is 'YYYY-MM-DD' (may be null); fall back to year."""
    raw = item.get("publication_date")
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            pass
    year = item.get("publication_year")
    if year:
        return datetime(int(year), 1, 1, tzinfo=UTC)
    return None


def _decode_abstract(inverted: dict | None) -> str | None:
    """Reconstruct plain text from OpenAlex's `abstract_inverted_index`.

    OpenAlex doesn't ship abstracts as text — it ships `{"word": [positions,
    ...]}`, a word -> list-of-positions map (their own inversion of a
    "position -> word" array, done to save space across millions of records).
    Flatten that back to (position, word) pairs, sort by position, and join.

    This is untrusted remote JSON, so every shape assumption is guarded
    rather than trusted: a non-list value under a word key is skipped instead
    of raising, and the rebuilt string is capped at _MAX_ABSTRACT_CHARS so a
    malformed index (e.g. a huge position value) can't produce an unbounded
    result.
    """
    if not inverted:
        return None
    pairs: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                pairs.append((pos, word))
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    text = " ".join(word for _, word in pairs)
    return text[:_MAX_ABSTRACT_CHARS] or None


def _issn_l(primary_location: dict) -> str | None:
    """Linking ISSN of the venue this work appeared in (Faz 5.3).

    `issn_l` is OpenAlex's own normalisation: one identifier per journal even
    when it has separate print and online ISSNs, which is exactly what the
    `journals` table keys on. A work with no venue — a preprint, a dataset —
    has no source object at all, which is normal.
    """
    source = (primary_location or {}).get("source") or {}
    issn = (source.get("issn_l") or "").strip()
    # String(9) in the column: "1234-567X" is 9 characters. Anything longer is
    # not an ISSN-L and would be truncated on write, so drop it instead.
    return issn if len(issn) == 9 else None


def _cited_by_count(item: dict) -> int | None:
    """Citations OpenAlex currently reports.

    None (not 0) when absent: "this response didn't include a count" and "this
    paper has no citations" are different claims, and `_enrich` treats them
    differently — see `_REFRESHABLE_FIELDS`.
    """
    raw = item.get("cited_by_count")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _to_payload(item: dict) -> PaperPayload | None:
    raw_id = item.get("id") or ""
    # "https://openalex.org/W2741809807" -> "W2741809807"
    external_id = raw_id.rsplit("/", 1)[-1] if raw_id else ""
    title = (item.get("display_name") or item.get("title") or "").strip()
    if not external_id or not title:
        return None

    primary_location = item.get("primary_location") or {}
    best_oa_location = item.get("best_oa_location") or {}
    doi = normalize_doi(item.get("doi"))

    # Prefer the DOI resolver link (stable, works for any reader), then the
    # publisher's own landing page, then fall back to the OpenAlex record
    # itself — the one URL guaranteed to exist for every item.
    url = item.get("doi") or primary_location.get("landing_page_url") or raw_id or None

    # `best_oa_location` is the open one; `primary_location` is the
    # publisher's and is routinely paywalled. Falling back to it filled
    # `pdf_url` with links a reader cannot open and, worse, left nothing able
    # to tell which rows were actually fetchable. `oa_*` below carries access
    # explicitly, and `pdf_url` keeps the fallback only as a display link.
    oa_pdf_url = best_oa_location.get("pdf_url")
    pdf_url = oa_pdf_url or primary_location.get("pdf_url")

    open_access = item.get("open_access") or {}
    oa_status = (open_access.get("oa_status") or "").strip().lower() or None
    # A location with no stated licence is a "no", not a "maybe" -- readable
    # does not imply redistributable.
    oa_license = (best_oa_location.get("license") or "").strip().lower() or None
    # Prefer the PDF, but a landing page is still a fetchable OA location and
    # trafilatura handles those; only `best_oa_location` is ever used here.
    oa_url = oa_pdf_url or best_oa_location.get("landing_page_url") or None

    authors = []
    for authorship in item.get("authorships") or []:
        # display_name can be present-but-null on anonymised authorships, so
        # `or ""` rather than a .get default — the default only covers a
        # missing key, not an explicit null.
        name = ((authorship.get("author") or {}).get("display_name") or "").strip()
        if name:
            authors.append(name)

    topics = [t.get("display_name") for t in (item.get("topics") or []) if t.get("display_name")]
    if not topics:
        topics = [
            c.get("display_name") for c in (item.get("concepts") or []) if c.get("display_name")
        ]

    return PaperPayload(
        source=SOURCE_NAME,
        external_id=external_id,
        title=title.replace("\n", " "),
        abstract=_decode_abstract(item.get("abstract_inverted_index")),
        authors=authors,
        url=url,
        pdf_url=pdf_url,
        published_at=_parse_date(item),
        categories=topics[:_MAX_CATEGORIES],
        doi=doi,
        issn_l=_issn_l(primary_location),
        cited_by_count=_cited_by_count(item),
        oa_status=oa_status,
        oa_license=oa_license,
        oa_url=oa_url,
    )


def search(query: str, *, max_results: int = 25) -> list[PaperPayload]:
    """One works-search request. Raises requests exceptions on HTTP
    failure; the caller (service layer) isolates per-source errors."""
    query = (query or "").strip()
    if not query:
        return []
    # OpenAlex's documented polite-pool budget (~10 req/s, 100k/day) is
    # per-deployment, not per-user scan — same reasoning as every other
    # bucket in ratelimit.py.
    if not openalex_slot():
        logger.warning("openalex_rate_limited", query=query)
        raise SourceThrottledError("openalex rate limit")
    resp = _session.get(
        _API_URL,
        params=_params(query, max_results),
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("results") or []
    out = [p for p in (_to_payload(i) for i in items) if p is not None]
    logger.info("openalex_search_done", query=query, hits=len(out))
    return out


def search_for_keywords(keywords: list[str], *, max_results: int = 25) -> list[PaperPayload]:
    """OpenAlex's `search` parameter handles boolean OR natively — one
    combined query per run, same shape as pubmed_source.search_for_keywords."""
    keywords = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not keywords:
        return []
    query = " OR ".join(keywords)
    return search(query, max_results=max_results)


# ----------------------------------------------------------------------------
# Author lookups (Faz 5.4) and DOI hydration (Faz 5.4, for Scopus)
# ----------------------------------------------------------------------------

_AUTHORS_URL = "https://api.openalex.org/authors"

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def normalize_orcid(value: str | None) -> str | None:
    """Reduce anything ORCID-shaped to the bare `0000-0002-1825-0097` form.

    Users paste the full `https://orcid.org/…` URL as often as the bare id,
    and sometimes without hyphens. Returns None when the result still is not
    a valid ORCID — callers treat that as "not an ORCID", not as an error.
    """
    if not value:
        return None
    candidate = value.strip().upper()
    for prefix in ("HTTPS://ORCID.ORG/", "HTTP://ORCID.ORG/", "ORCID.ORG/", "ORCID:"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    candidate = candidate.strip().strip("/")
    if "-" not in candidate and len(candidate) == 16:
        candidate = "-".join(candidate[i : i + 4] for i in range(0, 16, 4))
    return candidate if _ORCID_RE.match(candidate) else None


def _author_id(raw: str | None) -> str | None:
    """ "https://openalex.org/A5023888391" -> "A5023888391"."""
    if not raw:
        return None
    tail = str(raw).rsplit("/", 1)[-1].strip()
    return tail or None


def fetch_author(orcid_or_id: str) -> dict | None:
    """Resolve an ORCID or OpenAlex author id to `{"id", "name", "orcid",
    "works_count", "institution", "cited_by_count"}`.

    Called once, when a user follows someone — not per nightly run. That is
    the whole reason `UserAuthor.openalex_id` is stored: resolution is the
    expensive, failure-prone half, and doing it on follow means the user sees
    the error immediately instead of it vanishing into a task log.

    `institution` reuses `_author_institution` — the same field-shape
    tolerance `search_authors` needs (a `last_known_institutions` list on
    current responses, a single `last_known_institution` object on older
    ones) applies here too, since both endpoints return the same author
    resource. `cited_by_count` is a plain passthrough, same as `works_count`.

    Returns None when the identifier resolves to nothing (404 is OpenAlex's
    answer for an unknown ORCID, which is a normal outcome of a typo).
    """
    value = (orcid_or_id or "").strip()
    if not value:
        return None

    orcid = normalize_orcid(value)
    if orcid:
        url = f"{_AUTHORS_URL}/https://orcid.org/{orcid}"
    else:
        author_id = _author_id(value)
        if not author_id or not author_id.upper().startswith("A"):
            return None
        url = f"{_AUTHORS_URL}/{author_id}"

    if not openalex_slot():
        raise SourceThrottledError("openalex rate limit")
    resp = _session.get(url, headers=_headers(), timeout=_TIMEOUT)
    if resp.status_code == 404:
        logger.info("openalex_author_not_found", value=value)
        return None
    resp.raise_for_status()

    data = resp.json() or {}
    author_id = _author_id(data.get("id"))
    if not author_id:
        return None
    return {
        "id": author_id,
        "name": (data.get("display_name") or "").strip() or None,
        "orcid": normalize_orcid(data.get("orcid")),
        "works_count": data.get("works_count"),
        "institution": _author_institution(data),
        "cited_by_count": data.get("cited_by_count"),
    }


def works_by_author(
    author_id: str, *, since: datetime | None = None, max_results: int = 25
) -> list[PaperPayload]:
    """Recent works by one OpenAlex author, newest first.

    `since` filters server-side with `from_publication_date`, which is what
    keeps a prolific author from re-importing a career's output every night —
    the caller passes the high-water mark it already stored
    (`UserAuthor.last_work_at`).

    OpenAlex's date filter is day-granular and inclusive, so the newest
    already-seen day comes back again; `upsert_paper` collapses those to the
    existing rows, and `link_user_paper` reports them as not-new. Re-fetching
    one day is cheaper than tracking work ids.
    """
    author_id = _author_id(author_id)
    if not author_id:
        return []

    filters = [f"author.id:{author_id}"]
    if since is not None:
        filters.append(f"from_publication_date:{since.date().isoformat()}")

    params: dict[str, str | int] = {
        "filter": ",".join(filters),
        "per-page": min(max_results, _MAX_PER_PAGE),
        "sort": "publication_date:desc",
    }
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto

    if not openalex_slot():
        raise SourceThrottledError("openalex rate limit")
    resp = _session.get(_API_URL, params=params, headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()

    items = resp.json().get("results") or []
    out = [p for p in (_to_payload(i) for i in items) if p is not None]
    logger.info("openalex_author_works", author_id=author_id, hits=len(out))
    return out[:max_results]


# ----------------------------------------------------------------------------
# Reports (Faz 6) — multi-page aggregation, group-by statistics, author search
# ----------------------------------------------------------------------------

#: Backstop on the number of *pages* `works_in_range` will ever fetch,
#: independent of the rows cap below. `REPORT_MAX_WORKS // _MAX_PER_PAGE`
#: pages is enough in the ordinary case, but that ratio assumes every raw
#: result becomes one payload — a page where `_to_payload` skips a few
#: malformed records (missing title/id), or where the server doesn't fill a
#: page to `per-page`, would make net progress per page smaller than that,
#: and a tight ratio-based cap could then stop the loop before the rows cap
#: is ever reached. This constant is deliberately generous instead: large
#: enough that it never binds in normal operation, existing purely so a
#: misbehaving server cannot page forever.
_MAX_REPORT_PAGES = 50

#: Hard ceiling on how many works `works_in_range` will ever hand back. This
#: is a *reporting* path, not the per-scan `search()` above — a report can
#: legitimately ask for "everything published on X between 2020 and 2025",
#: and the live API note above measured a 5-year window at 94,759 hits for a
#: single query. Without a ceiling that is a report page trying to hold and
#: render tens of thousands of rows. `max_results` is clamped to this, never
#: the other way around.
REPORT_MAX_WORKS = 400

#: `group_by` dimensions this adapter has actually verified against the live
#: API (see the module's task notes) — OpenAlex accepts many more, but an
#: unverified one could silently return a shape `_aggregate_item` doesn't
#: expect. Reject anything else instead of guessing.
SUPPORTED_GROUP_BY = frozenset(
    {
        "publication_year",
        "authorships.author.id",
        "primary_location.source.id",
        "primary_topic.id",
        "open_access.is_oa",
    }
)


class UnsupportedGroupByError(ValueError):
    """Raised by `aggregate_works` for a `group_by` value this adapter has
    not verified — see `SUPPORTED_GROUP_BY`."""


def _date_range_filter(since_year: int, until_year: int) -> str:
    return f"from_publication_date:{since_year}-01-01,to_publication_date:{until_year}-12-31"


def works_in_range(
    query: str,
    *,
    since_year: int,
    until_year: int,
    sort: str = "cited_by_count:desc",
    max_results: int = 200,
) -> list[PaperPayload]:
    """Collect works for a report over a whole date range, paging with
    OpenAlex's cursor (`cursor=*` -> `meta.next_cursor`).

    This is the first multi-page adapter in this repo — every other adapter
    here answers one page and lets the caller decide whether to ask again.
    A report is different in kind: "every AI-safety paper from 2020-2025" is
    one logical request that happens to need several HTTP round trips, and a
    per-page cap (200, see `_MAX_PER_PAGE`) means a many-thousand-hit query
    would otherwise silently truncate to page one.

    Three independent guards make an infinite loop impossible even if the
    API misbehaves or `max_results` is passed in absurdly large:
      1. `max_results` is clamped to `REPORT_MAX_WORKS` before anything else,
         so the number of *rows* is always finite.
      2. The number of *pages* is separately capped at `_MAX_REPORT_PAGES`
         — belt-and-braces in case a page's net contribution to `out` ever
         falls short of `per-page` (skipped malformed records, a server that
         under-fills a page), which would make a tight rows/per-page ratio
         stop too early.
      3. The loop also stops the moment a page's `results` is empty, or its
         `meta.next_cursor` is missing/blank, or repeats the cursor just
         handed out — the last case guards against a misbehaving server
         that echoes the same cursor back forever instead of ending the
         sequence honestly.

    Every page spends one `openalex_slot()` token, same as every other call
    in this module — a 400-row report at 200/page is 2 requests, not a
    special-cased bulk exemption from the rate limit.
    """
    query = (query or "").strip()
    if not query:
        return []

    capped_max = max(0, min(max_results, REPORT_MAX_WORKS))
    if capped_max == 0:
        return []

    per_page = min(capped_max, _MAX_PER_PAGE)

    params: dict[str, str | int] = {
        "filter": _date_range_filter(since_year, until_year),
        "search": query,
        "sort": sort,
        "per-page": per_page,
    }
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto

    out: list[PaperPayload] = []
    cursor = "*"
    seen_cursors: set[str] = set()

    for _ in range(_MAX_REPORT_PAGES):
        if len(out) >= capped_max:
            break
        if not openalex_slot():
            logger.warning("openalex_rate_limited", query=query, stage="works_in_range")
            raise SourceThrottledError("openalex rate limit")

        page_params = dict(params)
        page_params["cursor"] = cursor
        resp = _session.get(_API_URL, params=page_params, headers=_headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json() or {}

        items = data.get("results") or []
        if not items:
            break
        for item in items:
            payload = _to_payload(item)
            if payload is not None:
                out.append(payload)
            if len(out) >= capped_max:
                break

        next_cursor = (data.get("meta") or {}).get("next_cursor")
        if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    logger.info(
        "openalex_works_in_range_done",
        query=query,
        since_year=since_year,
        until_year=until_year,
        hits=len(out),
    )
    return out[:capped_max]


def _aggregate_item(item: dict) -> dict:
    return {
        "key": item.get("key"),
        "name": item.get("key_display_name") or item.get("key"),
        "count": item.get("count"),
    }


def aggregate_works(query: str, *, since_year: int, until_year: int, group_by: str) -> list[dict]:
    """One `group_by` request, normalized to `{"key", "name", "count"}` rows.

    This is the LLM-free statistics path for reports — "top authors on this
    topic", "OA share over time" — answered directly from OpenAlex's own
    aggregation rather than fetching works and counting client-side (which
    would need every matching row, not just the <=200 a report can hold).

    `group_by` is restricted to `SUPPORTED_GROUP_BY`, the dimensions actually
    verified against the live API (see this module's task notes) — accepting
    an unverified value would mean a caller's typo returns an empty list
    that looks like "no results" instead of "no such dimension", which is a
    much worse failure to debug. So it raises instead.
    """
    query = (query or "").strip()
    if group_by not in SUPPORTED_GROUP_BY:
        raise UnsupportedGroupByError(f"unsupported group_by: {group_by!r}")

    params: dict[str, str | int] = {
        "filter": _date_range_filter(since_year, until_year),
        "group_by": group_by,
    }
    if query:
        params["search"] = query
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto

    if not openalex_slot():
        logger.warning("openalex_rate_limited", query=query, stage="aggregate_works")
        raise SourceThrottledError("openalex rate limit")

    resp = _session.get(_API_URL, params=params, headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json() or {}
    groups = data.get("group_by") or []
    out = [_aggregate_item(g) for g in groups]
    logger.info("openalex_aggregate_done", query=query, group_by=group_by, groups=len(out))
    return out


_MIN_AUTHOR_NAME_LEN = 2
_MAX_AUTHOR_TOPICS = 3


def _author_institution(item: dict) -> str | None:
    """OpenAlex has changed its shape here across API versions — a list under
    `last_known_institutions` (current) or a single object under
    `last_known_institution` (older docs/cached responses still show it).
    Tolerate both rather than betting on one."""
    institutions = item.get("last_known_institutions")
    if isinstance(institutions, list) and institutions:
        first = institutions[0]
        if isinstance(first, dict):
            name = (first.get("display_name") or "").strip()
            if name:
                return name

    single = item.get("last_known_institution")
    if isinstance(single, dict):
        name = (single.get("display_name") or "").strip()
        if name:
            return name

    return None


def _author_candidate(item: dict) -> dict | None:
    author_id = _author_id(item.get("id"))
    name = (item.get("display_name") or "").strip()
    if not author_id or not name:
        return None

    topics = [
        t.get("display_name")
        for t in (item.get("topics") or [])
        if isinstance(t, dict) and t.get("display_name")
    ][:_MAX_AUTHOR_TOPICS]

    return {
        "id": author_id,
        "name": name,
        "orcid": normalize_orcid(item.get("orcid")),
        "institution": _author_institution(item),
        "works_count": item.get("works_count"),
        "cited_by_count": item.get("cited_by_count"),
        "topics": topics,
    }


def search_authors(name: str, *, limit: int = 10) -> list[dict]:
    """Candidate authors by free-text name, for a human to pick from.

    Author-following in this app has, until now, *deliberately* refused a
    free-text name search — `forms.py`'s docstring called it out explicitly
    ("Deliberately not a free-text name search") on the grounds that picking
    an auto-matched "J. Smith" would silently pollute a user's feed with the
    wrong person's papers. That reasoning still holds against *auto*-picking
    a match; it does not hold against showing a human a short list of
    candidates and letting them choose. So this function returns candidates
    only — it never chooses one itself. Institution, works/citation counts,
    and topics exist on each candidate precisely because those are what let
    a person tell "J. Smith the immunologist" apart from "J. Smith the
    astrophysicist" without opening OpenAlex in a separate tab.

    Returns `[]` for a blank/too-short name (no request made — not worth a
    slot for input that cannot plausibly be a real query) and for a 404 or an
    empty result set, all normal outcomes rather than errors.
    """
    name = (name or "").strip()
    if len(name) < _MIN_AUTHOR_NAME_LEN:
        return []

    params: dict[str, str | int] = {"search": name, "per-page": max(1, limit)}
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto

    if not openalex_slot():
        raise SourceThrottledError("openalex rate limit")

    resp = _session.get(_AUTHORS_URL, params=params, headers=_headers(), timeout=_TIMEOUT)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()

    items = (resp.json() or {}).get("results") or []
    out = [c for c in (_author_candidate(i) for i in items) if c is not None]
    logger.info("openalex_search_authors_done", name=name, hits=len(out))
    return out[:limit]


def fetch_by_doi(doi: str) -> PaperPayload | None:
    """One work, by DOI (Faz 5.4).

    Exists for the Scopus path: Scopus tells us a DOI exists but its licence
    forbids storing the abstract, so the storable metadata is fetched from
    OpenAlex instead. See `scopus_source` and
    `docs/adr/0002-elsevier-discovery-only.md`.

    Returns None for an unknown DOI — OpenAlex does not have everything, and
    that is a normal outcome rather than a failure.
    """
    normalized = normalize_doi(doi)
    if not normalized:
        return None
    if not openalex_slot():
        raise SourceThrottledError("openalex rate limit")

    resp = _session.get(
        f"{_API_URL}/https://doi.org/{normalized}",
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return _to_payload(resp.json() or {})
