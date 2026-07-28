"""Semantic Scholar Graph API adapter.

Uses the public relevance-search endpoint:
  https://api.semanticscholar.org/graph/v1/paper/search

Works without an API key (shared ~100 req / 5 min pool); set
SEMANTIC_SCHOLAR_API_KEY for a dedicated quota. The endpoint is plain-text
relevance search — no boolean OR — so `search_for_keywords` issues one
request per keyword and splits the result budget across them.

Same contract as the other adapters: pure I/O, returns PaperPayload rows,
never raises for a single bad record (skips it), raises requests exceptions
upward so the service layer can isolate a failing source.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import requests
import structlog

from app.modules.scrape.ratelimit import SourceThrottledError, semantic_scholar_slot
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "semantic_scholar"

_API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_FIELDS = "title,abstract,authors,url,openAccessPdf,publicationDate,year,externalIds,fieldsOfStudy"
_TIMEOUT = 20  # seconds


def _headers() -> dict[str, str]:
    key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    return {"x-api-key": key} if key else {}


def _parse_date(item: dict) -> datetime | None:
    """publicationDate is 'YYYY-MM-DD' (may be null); fall back to year."""
    raw = item.get("publicationDate")
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            pass
    year = item.get("year")
    if year:
        return datetime(int(year), 1, 1, tzinfo=UTC)
    return None


def _to_payload(item: dict) -> PaperPayload | None:
    paper_id = item.get("paperId")
    title = (item.get("title") or "").strip()
    if not paper_id or not title:
        return None
    pdf = item.get("openAccessPdf") or {}
    return PaperPayload(
        source=SOURCE_NAME,
        external_id=paper_id,
        title=title.replace("\n", " "),
        abstract=(item.get("abstract") or "").strip() or None,
        authors=[a.get("name", "") for a in (item.get("authors") or []) if a.get("name")],
        url=item.get("url"),
        pdf_url=pdf.get("url"),
        published_at=_parse_date(item),
        categories=list(item.get("fieldsOfStudy") or []),
    )


def search(query: str, *, max_results: int = 25) -> list[PaperPayload]:
    """One relevance-search request. Raises requests exceptions on HTTP
    failure; the caller (service layer) isolates per-source errors."""
    query = (query or "").strip()
    if not query:
        return []
    # The published quota is per API key / per IP, i.e. shared by every worker.
    # Without this, N concurrent per-user scans multiply it by N.
    if not semantic_scholar_slot():
        logger.warning("semantic_scholar_rate_limited", query=query)
        raise SourceThrottledError("semantic scholar rate limit")
    resp = requests.get(
        _API_URL,
        params={"query": query, "limit": min(max_results, 100), "fields": _FIELDS},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("data") or []
    out = [p for p in (_to_payload(i) for i in items) if p is not None]
    logger.info("semantic_scholar_search_done", query=query, hits=len(out))
    return out


def search_for_keywords(keywords: list[str], *, max_results: int = 25) -> list[PaperPayload]:
    """One request per keyword (the API has no OR operator), budget split
    evenly. A keyword that 429s/fails is logged and skipped so the other
    keywords still land.

    But if *every* keyword failed and we collected nothing, the failure is
    raised instead of being reported as an empty result. This endpoint is
    unauthenticated by default and shares a ~100 req / 5 min pool with every
    other caller on the same IP, so 429 is the normal outcome of pressing
    "Scrape now" twice in a row — and swallowing it made the scan record
    `status="ok", hits=0`, i.e. told the user "there are no papers on your
    topic" when the truth was "we never got to ask". Set
    SEMANTIC_SCHOLAR_API_KEY for a dedicated quota.
    """
    keywords = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not keywords:
        return []
    per_kw = max(3, max_results // len(keywords))
    out: list[PaperPayload] = []
    seen: set[str] = set()
    last_error: Exception | None = None
    for kw in keywords:
        try:
            hits = search(kw, max_results=per_kw)
        except (requests.RequestException, SourceThrottledError) as exc:
            logger.warning("semantic_scholar_keyword_failed", keyword=kw, error=str(exc))
            last_error = exc
            continue
        for p in hits:
            if p.external_id not in seen:
                seen.add(p.external_id)
                out.append(p)
    if last_error is not None and not out:
        raise last_error
    return out[:max_results]
