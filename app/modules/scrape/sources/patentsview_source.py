"""PatentsView (USPTO) adapter — US patents, richly structured.

The counterpart to `epo_ops_source`: narrower coverage (US only) but far
better structure. Where EPO gives one flat biblio record, PatentsView returns
inventors, assignees and CPC codes as first-class related objects, which is
what makes "who else is working on this" answerable rather than guessable.

Free tier: a key from PatentsView (no OAuth dance — a plain `X-Api-Key`
header) and 45 requests/minute.

Query shape is PatentsView's own JSON DSL rather than a query string:

    q = {"_or": [{"_text_any": {"patent_title": "kw"}}, ...]}
    f = ["patent_id", "patent_title", ...]     # fields to return
    o = {"size": 25}                           # options

`_or` means the whole keyword set goes out in **one** request, so — like
`epo_ops` and unlike `semantic_scholar`/`crossref` — this source is not in
`service._PER_KEYWORD_REQUEST_SOURCES`.

429 carries a `Retry-After`; the adapter honours it once and then gives up
rather than sitting on a worker for an arbitrary wait, because the nightly
task has its own soft time limit and a source that is rate limited is a
legitimate `partial` outcome.

Same contract as every other adapter: pure I/O, PaperPayload rows, a single
unparseable record is skipped rather than fatal, transport errors propagate.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime

import requests
import structlog

from app.modules.scrape.ratelimit import SourceThrottledError, consume_quota, patentsview_slot
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "patentsview"

_API_URL = "https://search.patentsview.org/api/v1/patent/"
_GOOGLE_PATENTS_URL = "https://patents.google.com/patent/US{patent_id}"

_TIMEOUT = 20  # seconds
_MAX_SIZE = 100  # PatentsView caps `o.size` at 1000; 100 matches our other adapters
_MAX_CATEGORIES = 8

#: Longest we will honour a 429's Retry-After before giving up. The nightly
#: task has its own soft time limit, and "rate limited" is a legitimate
#: partial outcome — parking a worker for a minute to avoid saying so is a bad
#: trade.
_MAX_RETRY_AFTER = 10.0

_FIELDS = [
    "patent_id",
    "patent_title",
    "patent_abstract",
    "patent_date",
    "inventors.inventor_name_first",
    "inventors.inventor_name_last",
    "assignees.assignee_organization",
    "cpc_current.cpc_group_id",
]


def credentials_ok() -> bool:
    """Read per call, not captured at import — see `sources.credentials_ok`."""
    return bool(os.getenv("PATENTSVIEW_API_KEY"))


def build_query(keywords: list[str]) -> dict:
    """PatentsView's JSON DSL for "title or abstract mentions any of these".

    `_text_any` is PatentsView's tokenised any-of-these-words match, and `_or`
    combines the per-keyword clauses — which is what lets the whole keyword
    set travel in one request.
    """
    clauses: list[dict] = []
    for kw in keywords:
        cleaned = (kw or "").strip()
        if not cleaned:
            continue
        clauses.append({"_text_any": {"patent_title": cleaned}})
        clauses.append({"_text_any": {"patent_abstract": cleaned}})
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"_or": clauses}


def _inventors(patent: dict) -> list[str]:
    out: list[str] = []
    for inv in patent.get("inventors") or []:
        if not isinstance(inv, dict):
            continue
        first = (inv.get("inventor_name_first") or "").strip()
        last = (inv.get("inventor_name_last") or "").strip()
        name = f"{first} {last}".strip()
        if name and name not in out:
            out.append(name)
    return out


def _assignees(patent: dict) -> list[str]:
    """Assignee organisations — the "who owns this" signal that makes the
    industry side of a topic visible. Individual (unassigned) patents have
    none, which is normal, not an error."""
    out: list[str] = []
    for a in patent.get("assignees") or []:
        if not isinstance(a, dict):
            continue
        org = (a.get("assignee_organization") or "").strip()
        if org and org not in out:
            out.append(org)
    return out


def _cpc_codes(patent: dict) -> list[str]:
    out: list[str] = []
    for cpc in patent.get("cpc_current") or []:
        if not isinstance(cpc, dict):
            continue
        code = (cpc.get("cpc_group_id") or "").strip()
        if code and code not in out:
            out.append(code)
    return out[:_MAX_CATEGORIES]


def _parse_date(raw: str | None) -> datetime | None:
    """`patent_date` is ISO `YYYY-MM-DD`."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _to_payload(patent: dict) -> PaperPayload | None:
    patent_id = str(patent.get("patent_id") or "").strip()
    if not patent_id:
        return None
    title = (patent.get("patent_title") or "").strip()
    if not title:
        return None

    # Assignees ride along in `categories` rather than `authors`: an
    # organisation is not an inventor, and the card renders authors as people.
    # Prefixed so the two are tellable apart in the category list.
    categories = _cpc_codes(patent) + [f"assignee:{o}" for o in _assignees(patent)]

    return PaperPayload(
        source=SOURCE_NAME,
        external_id=f"US{patent_id}",
        title=title.replace("\n", " "),
        abstract=(patent.get("patent_abstract") or "").strip() or None,
        authors=_inventors(patent),
        url=_GOOGLE_PATENTS_URL.format(patent_id=patent_id),
        pdf_url=None,
        published_at=_parse_date(patent.get("patent_date")),
        categories=categories[:_MAX_CATEGORIES],
        kind="patent",
        doi=None,  # patents have no DOI — see epo_ops_source
    )


def _get(params: dict) -> requests.Response:
    """One request, honouring a single bounded `Retry-After` on 429."""
    resp = requests.get(
        _API_URL,
        params=params,
        headers={"X-Api-Key": os.getenv("PATENTSVIEW_API_KEY", "")},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 429:
        return resp

    try:
        wait = float(resp.headers.get("Retry-After", "0"))
    except (TypeError, ValueError):
        wait = 0.0
    if wait <= 0 or wait > _MAX_RETRY_AFTER:
        logger.warning("patentsview_rate_limited", retry_after=wait)
        raise SourceThrottledError("patentsview rate limit")

    logger.info("patentsview_retry_after", seconds=wait)
    time.sleep(wait)
    return requests.get(
        _API_URL,
        params=params,
        headers={"X-Api-Key": os.getenv("PATENTSVIEW_API_KEY", "")},
        timeout=_TIMEOUT,
    )


def search(query: dict | str, *, max_results: int = 25) -> list[PaperPayload]:
    """One search. `query` is a PatentsView `q` object (or a bare keyword,
    which is wrapped into one).

    Raises `SourceThrottledError` rather than returning `[]` when throttled or
    out of budget — see `ratelimit.SourceThrottledError`.
    """
    if isinstance(query, str):
        query = build_query([query])
    if not query:
        return []
    if not credentials_ok():
        raise SourceThrottledError("patentsview credentials missing")
    if not patentsview_slot():
        logger.warning("patentsview_rate_limited")
        raise SourceThrottledError("patentsview rate limit")
    if not consume_quota(SOURCE_NAME, cost=1):
        logger.warning("patentsview_quota_exhausted")
        raise SourceThrottledError("patentsview weekly quota exhausted")

    params = {
        "q": json.dumps(query),
        "f": json.dumps(_FIELDS),
        "o": json.dumps({"size": min(max_results, _MAX_SIZE)}),
    }
    resp = _get(params)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()

    patents = (resp.json() or {}).get("patents") or []
    out = [p for p in (_to_payload(p) for p in patents) if p is not None]
    logger.info("patentsview_search_done", hits=len(out))
    return out[:max_results]


def search_for_keywords(keywords: list[str], *, max_results: int = 25) -> list[PaperPayload]:
    """All keywords in **one** request via `_or` — same shape as `epo_ops`,
    which is why neither is in `_PER_KEYWORD_REQUEST_SOURCES`."""
    query = build_query([kw for kw in keywords if kw and kw.strip()])
    if not query:
        return []
    return search(query, max_results=min(max_results, _MAX_SIZE))
