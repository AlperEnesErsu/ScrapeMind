"""Scopus adapter — **discovery only**, off by default.

Read `docs/adr/0002-elsevier-discovery-only.md` before changing anything here.

What this source is for
-----------------------
Scopus indexes work the open catalogues miss, particularly outside English-
language publishing. It is useful for answering *"does something on this topic
exist?"* — and that is the only question it is allowed to answer here.

The rule that shapes the whole module
-------------------------------------
**No Elsevier-licensed content is ever persisted.** The payload carries a DOI,
a title, a date and a link back to Scopus. `abstract` is hard-wired to None —
not "usually empty", not "trimmed", None. Storable metadata comes from
OpenAlex instead, through the DOI: `service._hydrate_scopus_payloads` calls
`openalex_source.fetch_by_doi` and lets the existing DOI-first `upsert_paper`
merge the two. So Scopus says *what exists*; OpenAlex supplies *what we keep*.

The title is the one piece of Elsevier-sourced text that does get stored, and
that is a deliberate, documented residual risk rather than an oversight — see
the ADR. A row with no OpenAlex match keeps its Scopus title and links out.

Gating
------
Requires `SCOPUS_API_KEY` *and* an admin turning on `scopus_enabled`. Default
off. The key is bound to an institution's IP range, so a deployment outside
that range gets nothing useful even with a valid key — `SCOPUS_INSTTOKEN`
exists for off-campus access and is passed when set.

Metered at 20.000 requests/week through `ratelimit.consume_quota`, which is
fail-closed: no budget, no request.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import requests
import structlog

from app.modules.scrape.doi import normalize_doi
from app.modules.scrape.ratelimit import SourceThrottledError, consume_quota, scopus_slot
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "scopus"

_API_URL = "https://api.elsevier.com/content/search/scopus"
_TIMEOUT = 20  # seconds
_MAX_RESULTS = 25  # Scopus caps `count` at 25 for the standard view


def credentials_ok() -> bool:
    """Read per call — see `sources.credentials_ok`. The insttoken is
    optional: on-campus deployments do not need one."""
    return bool(os.getenv("SCOPUS_API_KEY"))


def _headers() -> dict[str, str]:
    headers = {
        "X-ELS-APIKey": os.getenv("SCOPUS_API_KEY", ""),
        "Accept": "application/json",
    }
    insttoken = os.getenv("SCOPUS_INSTTOKEN", "").strip()
    if insttoken:
        headers["X-ELS-Insttoken"] = insttoken
    return headers


def build_query(keywords: list[str]) -> str:
    """`TITLE-ABS-KEY(a OR b)` — Scopus supports boolean OR, so the whole
    keyword set travels in one request (like EPO and PatentsView, and unlike
    Semantic Scholar/Crossref).

    Double quotes are stripped rather than escaped: inside `TITLE-ABS-KEY` a
    stray quote changes the query's meaning, and a keyword containing one is a
    user typo, not an operator.
    """
    terms = [kw.replace('"', "").strip() for kw in keywords if kw and kw.strip()]
    terms = [t for t in terms if t]
    if not terms:
        return ""
    return "TITLE-ABS-KEY(" + " OR ".join(f'"{t}"' for t in terms) + ")"


def _scopus_url(entry: dict) -> str | None:
    """The Scopus record link — a link-out, which is what the licence allows
    in place of reproducing content."""
    for link in entry.get("link") or []:
        if isinstance(link, dict) and link.get("@ref") == "scopus":
            return link.get("@href")
    return None


def _parse_date(raw: str | None) -> datetime | None:
    """`prism:coverDate` is ISO `YYYY-MM-DD`."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _to_payload(entry: dict) -> PaperPayload | None:
    """Discovery payload: identity and a link, nothing else.

    A record without a DOI is **skipped**, not kept with the Scopus id as its
    identity. The DOI is the whole hydration mechanism — without one there is
    no way to fetch storable metadata from OpenAlex, so keeping the row would
    mean persisting Elsevier's title with nothing else and no way to improve
    it.
    """
    doi = normalize_doi(entry.get("prism:doi"))
    if not doi:
        return None
    title = (entry.get("dc:title") or "").strip()
    if not title:
        return None

    return PaperPayload(
        source=SOURCE_NAME,
        external_id=doi,
        title=title.replace("\n", " "),
        # Hard None, deliberately. See the module docstring and ADR-0002.
        abstract=None,
        authors=[],
        url=_scopus_url(entry),
        pdf_url=None,
        published_at=_parse_date(entry.get("prism:coverDate")),
        categories=[],
        doi=doi,
    )


def search(query: str, *, max_results: int = 25) -> list[PaperPayload]:
    """One Scopus search. Raises `SourceThrottledError` rather than returning
    `[]` when uncredentialed, throttled or out of budget — an empty list would
    be recorded as a clean scan that found nothing."""
    query = (query or "").strip()
    if not query:
        return []
    if not credentials_ok():
        raise SourceThrottledError("scopus credentials missing")
    if not scopus_slot():
        logger.warning("scopus_rate_limited")
        raise SourceThrottledError("scopus rate limit")
    if not consume_quota(SOURCE_NAME, cost=1):
        logger.warning("scopus_quota_exhausted")
        raise SourceThrottledError("scopus weekly quota exhausted")

    resp = requests.get(
        _API_URL,
        params={"query": query, "count": min(max_results, _MAX_RESULTS)},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    if resp.status_code in (401, 403):
        # Almost always the institution-IP binding rather than a bad key —
        # worth its own log line, because "valid key, wrong network" is the
        # confusing failure here.
        logger.warning("scopus_unauthorized", status=resp.status_code)
        raise SourceThrottledError("scopus rejected the request (key or IP binding)")
    if resp.status_code == 404:
        return []
    resp.raise_for_status()

    entries = ((resp.json() or {}).get("search-results") or {}).get("entry") or []
    out = [p for p in (_to_payload(e) for e in entries) if p is not None]
    logger.info("scopus_search_done", hits=len(out))
    return out[:max_results]


def search_for_keywords(keywords: list[str], *, max_results: int = 25) -> list[PaperPayload]:
    """All keywords in one `TITLE-ABS-KEY(... OR ...)` request."""
    query = build_query(keywords)
    if not query:
        return []
    return search(query, max_results=max_results)
