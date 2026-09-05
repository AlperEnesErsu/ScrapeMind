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

    pdf_url = best_oa_location.get("pdf_url") or primary_location.get("pdf_url")

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
    "works_count"}`.

    Called once, when a user follows someone — not per nightly run. That is
    the whole reason `UserAuthor.openalex_id` is stored: resolution is the
    expensive, failure-prone half, and doing it on follow means the user sees
    the error immediately instead of it vanishing into a task log.

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
