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
    resp = requests.get(
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
