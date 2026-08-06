"""Crossref adapter.

Uses the public works-search endpoint:
  https://api.crossref.org/works

No API key exists or is needed — Crossref is the DOI registration agency for
most scholarly publishers, and its search API is fully open. What it does
gate on is *politeness*, the same deal as OpenAlex: requests that identify a
contact address (via `mailto=` and, per Crossref's own recommendation, a
matching User-Agent) are routed into the "polite pool", which gets faster
and more consistent response times than the shared anonymous pool. Without
CROSSREF_MAILTO set, requests still work, they just share that anonymous
pool with every other uncredentialed caller on the internet.

`query.bibliographic` is bag-of-words relevance scoring with no boolean OR
(unlike OpenAlex/PubMed), so `search_for_keywords` issues one request per
keyword — same shape as `semantic_scholar_source.search_for_keywords`.

Crossref abstracts, when present, are JATS XML fragments (e.g.
`<jats:p>...</jats:p>`); `_strip_jats` reduces them to plain text the same
way `rss_source._strip_html` does for RSS summaries — a compiled tag-regex,
not a dependency. Coverage is partial: many publishers never deposit an
abstract at all, so Crossref's practical value here is mostly DOI +
metadata enrichment (and dedup via `doi.normalize_doi`), not text.

Same contract as the other adapters: pure I/O, returns PaperPayload rows,
never raises for a single bad record (skips it), raises requests exceptions
upward so the service layer can isolate a failing source.
"""

from __future__ import annotations

import html
import os
import re
from datetime import UTC, datetime

import requests
import structlog

from app.modules.scrape.doi import normalize_doi
from app.modules.scrape.ratelimit import SourceThrottledError, crossref_slot
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "crossref"

_API_URL = "https://api.crossref.org/works"
_TIMEOUT = 20  # seconds

# Crossref caps `rows` at 100 (mirrors OpenAlex's _MAX_PER_PAGE / S2's `limit`).
_MAX_ROWS = 100

# PubMed caps its MeSH terms at 8 (see pubmed_source._parse_article); match
# that so no single source dominates a paper's category list.
_MAX_CATEGORIES = 8

# Only the fields the adapter actually consumes — smaller responses, and one
# less place a Crossref schema change can silently start returning nothing.
_SELECT = "DOI,title,abstract,author,issued,URL,link,subject,type,container-title"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ABSTRACT_HEADING_RE = re.compile(r"^Abstract\s*[:.]?\s*", re.IGNORECASE)


def _mailto() -> str:
    return os.getenv("CROSSREF_MAILTO", "")


def _params(query: str, max_results: int) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "query.bibliographic": query,
        "rows": min(max_results, _MAX_ROWS),
        "select": _SELECT,
    }
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto
    return params


def _headers() -> dict[str, str]:
    mailto = _mailto()
    return {"User-Agent": f"ScrapeMind (mailto:{mailto})"} if mailto else {}


def _strip_jats(raw: str | None) -> str | None:
    """Best-effort plain-text abstract from a JATS XML fragment.

    Crossref abstracts (when deposited at all) come wrapped as
    `<jats:p>...</jats:p>` and friends, not plain text. Same approach as
    `rss_source._strip_html` and for the same reason — this is a small,
    forgiving text extraction, not a renderable-fragment parser, so a
    regex-based tag strip is the right amount of machinery (no lxml/bs4
    dependency for it).
    """
    if not raw:
        return None
    cleaned = _TAG_RE.sub(" ", raw)
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    cleaned = _ABSTRACT_HEADING_RE.sub("", cleaned).strip()
    return cleaned or None


def _date_from_parts(container: dict | None) -> datetime | None:
    """`{"date-parts": [[Y]]}` / `[[Y, M]]` / `[[Y, M, D]]` -> tz-aware UTC
    datetime, missing month/day defaulting to 1.

    `date-parts` is untrusted remote JSON — a `[[None]]` entry really occurs
    for some records — so any shape/type surprise falls through to None
    instead of raising.
    """
    if not container:
        return None
    parts_list = container.get("date-parts")
    if not parts_list:
        return None
    parts = parts_list[0]
    if not parts:
        return None
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        day = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        return datetime(year, month, day, tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        return None


def _parse_date(item: dict) -> datetime | None:
    """`issued` is Crossref's preferred date; some records only carry
    `published-print` or `published-online` instead."""
    for key in ("issued", "published-print", "published-online"):
        dt = _date_from_parts(item.get(key))
        if dt is not None:
            return dt
    return None


def _author_name(author: dict) -> str:
    """given+family for personal authors; `name` for organisational ones
    (Crossref uses this for consortia/collaborations that have no given/
    family split)."""
    given = (author.get("given") or "").strip()
    family = (author.get("family") or "").strip()
    full = f"{given} {family}".strip()
    if full:
        return full
    return (author.get("name") or "").strip()


def _pdf_url(links: list | None) -> str | None:
    """`link` is a list of `{"URL": ..., "content-type": ...}`; take the
    first entry Crossref itself labels as a PDF. Entries are untrusted
    remote JSON, so non-dict items are skipped rather than raising."""
    for link in links or []:
        if not isinstance(link, dict):
            continue
        if link.get("content-type") == "application/pdf":
            return link.get("URL")
    return None


def _title(item: dict) -> str | None:
    """`title` is a list (Crossref's schema allows more than one, e.g. a
    subtitle as a second entry) — take the first non-empty one."""
    for raw in item.get("title") or []:
        title = (raw or "").strip()
        if title:
            return title.replace("\n", " ")
    return None


def _to_payload(item: dict) -> PaperPayload | None:
    # The DOI *is* Crossref's identity for a work — a record without one has
    # nothing stable to key on, so it's skipped rather than kept with a
    # made-up id.
    doi = normalize_doi(item.get("DOI"))
    if not doi:
        return None
    title = _title(item)
    if not title:
        return None

    authors = [n for n in (_author_name(a) for a in item.get("author") or []) if n]

    return PaperPayload(
        source=SOURCE_NAME,
        external_id=doi,
        title=title,
        abstract=_strip_jats(item.get("abstract")),
        authors=authors,
        url=item.get("URL"),
        pdf_url=_pdf_url(item.get("link")),
        published_at=_parse_date(item),
        categories=[c for c in (item.get("subject") or []) if c][:_MAX_CATEGORIES],
        doi=doi,
    )


def search(query: str, *, max_results: int = 25) -> list[PaperPayload]:
    """One works-search request. Raises requests exceptions on HTTP
    failure; the caller (service layer) isolates per-source errors."""
    query = (query or "").strip()
    if not query:
        return []
    # Crossref's polite-pool budget is per-deployment, not per-user scan —
    # same reasoning as every other bucket in ratelimit.py.
    if not crossref_slot():
        logger.warning("crossref_rate_limited", query=query)
        raise SourceThrottledError("crossref rate limit")
    resp = requests.get(
        _API_URL,
        params=_params(query, max_results),
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("message", {}).get("items") or []
    out = [p for p in (_to_payload(i) for i in items) if p is not None]
    logger.info("crossref_search_done", query=query, hits=len(out))
    return out


def search_for_keywords(keywords: list[str], *, max_results: int = 25) -> list[PaperPayload]:
    """One request per keyword (`query.bibliographic` is bag-of-words
    relevance scoring with no boolean OR — unlike OpenAlex/PubMed, there is
    no way to combine keywords into a single query and get back "matches any
    of these", so this mirrors
    `semantic_scholar_source.search_for_keywords`), budget split evenly. A
    keyword that fails is logged and skipped so the other keywords still
    land.

    But if *every* keyword failed and we collected nothing, the failure is
    raised instead of being reported as an empty result. Crossref's public
    pool is unauthenticated and shared by every caller who hasn't set
    CROSSREF_MAILTO, so a run of 429s/5xxs is a plausible outcome of a busy
    period, not evidence that the user's topic has zero matching works.
    Swallowing it would have recorded the scan as `status="ok", hits=0` —
    telling the user "there is nothing on your topic" when the truth is "we
    never got to ask".
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
            logger.warning("crossref_keyword_failed", keyword=kw, error=str(exc))
            last_error = exc
            continue
        for p in hits:
            if p.external_id not in seen:
                seen.add(p.external_id)
                out.append(p)
    if last_error is not None and not out:
        raise last_error
    return out[:max_results]
