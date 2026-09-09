"""EPO Open Patent Services (OPS) v3.2 adapter — patents, worldwide.

Why this source at all: every academic adapter answers the same question
("who published on this?"). None of them answers "who is *building* this, and
has my idea already been claimed?". EPO OPS covers DOCDB — worldwide
publications including TR — with abstracts, patent families and legal status,
on a free tier of 4 GB/week.

Three things make this adapter different from the five before it.

**It is the first source that authenticates.** OPS uses OAuth2
client-credentials: POST the key/secret as HTTP Basic to `/auth/accesstoken`,
get a bearer token good for 20 minutes. The token is cached at module level
and refreshed once on a 401 (a token can expire between the check and the
call; retrying once is cheaper and more honest than trying to predict clock
skew). Refresh logic stays *here* rather than in a shared wrapper — the
adapters deliberately use `requests` at module level so tests can monkeypatch
each module's own `requests` (CLAUDE.md rule 7), and hiding auth behind a
common helper would break that.

For the same reason the official `python-epo-ops-client` SDK is not used.

**It meters bandwidth, not calls.** The free tier is 4 GB/week, so every
response's byte count is charged against `ratelimit.consume_quota(bytes_=...)`
after the fact — we cannot know a response's size before reading it, so the
budget is checked before the call (with a nominal cost) and the real size is
settled after. Overshooting the ceiling by one response is acceptable; the
alternative is not calling at all.

**CQL supports boolean OR**, so all of a user's keywords go out in one
request: `ti,ab any "kw1" or ti,ab any "kw2"`. That is why `epo_ops` is
deliberately *not* in `service._PER_KEYWORD_REQUEST_SOURCES` — that set also
feeds the scan-duration estimate shown to users, and listing it there would
inflate the estimate by a factor of the keyword count.

Same contract as every other adapter: pure I/O, returns PaperPayload rows,
skips a single unparseable record rather than raising, and lets transport
errors propagate so the service layer can isolate a failing source.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import requests
import structlog

from app.modules.scrape.ratelimit import SourceThrottledError, consume_quota, epo_ops_slot
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "epo_ops"

_AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
_SEARCH_URL = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"
_ESPACENET_URL = "https://worldwide.espacenet.com/patent/search?q=pn%3D{pn}"

_TIMEOUT = 25  # seconds — OPS is slower than the metadata APIs

# OPS caps a biblio search at 100 results per request and 25 per page.
_MAX_RESULTS = 100
_PAGE_SIZE = 25

# Mirrors crossref/pubmed: no single source should dominate a paper's
# category list. Here these are CPC/IPC classification codes.
_MAX_CATEGORIES = 8

# Tokens are valid for 20 minutes. Refresh a minute early so a request that
# starts just under the wire does not race the expiry.
_TOKEN_TTL = 20 * 60
_TOKEN_SKEW = 60

#: Nominal per-request byte charge, used to reserve budget *before* a call
#: whose real size is only knowable afterwards. Roughly one 25-result biblio
#: page; the true figure is settled once the response is in hand.
_NOMINAL_BYTES = 60_000

#: {"value": str, "expires_at": float}. Module-level on purpose — one token
#: per worker process, shared by every user's scan in that process.
_token_cache: dict[str, object] = {}


def credentials_ok() -> bool:
    """Both halves of the client-credentials pair must be present.

    Read from the environment on every call (not captured at import) so a
    deployment that adds the key does not need a restart — see
    `sources.credentials_ok`.
    """
    return bool(os.getenv("EPO_OPS_KEY") and os.getenv("EPO_OPS_SECRET"))


def _fetch_token() -> str:
    """Exchange key/secret for a bearer token. Raises on failure."""
    resp = requests.post(
        _AUTH_URL,
        data={"grant_type": "client_credentials"},
        auth=(os.getenv("EPO_OPS_KEY", ""), os.getenv("EPO_OPS_SECRET", "")),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    token = (resp.json() or {}).get("access_token")
    if not token:
        raise ValueError("EPO OPS returned no access_token")
    _token_cache["value"] = token
    _token_cache["expires_at"] = time.time() + _TOKEN_TTL - _TOKEN_SKEW
    logger.info("epo_ops_token_refreshed")
    return token


def _token(*, force_refresh: bool = False) -> str:
    """Cached bearer token, refreshed when stale or when forced by a 401."""
    if not force_refresh:
        cached = _token_cache.get("value")
        expires_at = _token_cache.get("expires_at")
        if cached and isinstance(expires_at, int | float) and time.time() < expires_at:
            return str(cached)
    return _fetch_token()


def reset_token_cache() -> None:
    """Drop the cached token. Only used by tests and by a credential change."""
    _token_cache.clear()


def build_cql(keywords: list[str]) -> str:
    """CQL for "title or abstract mentions any of these keywords".

    OPS speaks CQL, which supports boolean OR — the reason this adapter sends
    one request for the whole keyword set instead of one per keyword. Double
    quotes inside a term are dropped rather than escaped: CQL has no escape
    sequence for them inside a quoted string, and a keyword containing a quote
    is a user typo, not a query operator.
    """
    terms = []
    for kw in keywords:
        cleaned = (kw or "").replace('"', "").strip()
        if cleaned:
            terms.append(f'ti,ab any "{cleaned}"')
    return " or ".join(terms)


def _as_list(value) -> list:
    """OPS collapses single-element arrays into the bare object, so nearly
    every nested field is "a dict or a list of dicts". Normalising once here
    keeps that quirk out of the parsing functions."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _text(node) -> str:
    """OPS wraps text as `{"$": "the text"}`; plain strings also occur."""
    if isinstance(node, dict):
        return str(node.get("$", "")).strip()
    if isinstance(node, str):
        return node.strip()
    return ""


def _publication_number(doc: dict) -> str | None:
    """`CC` + `doc-number` + `kind` from the docdb publication reference, e.g.
    "EP1000000A1" — OPS's stable identity for a publication."""
    for ref in _as_list((doc.get("bibliographic-data") or {}).get("publication-reference")):
        for doc_id in _as_list(ref.get("document-id")):
            if doc_id.get("@document-id-type") != "docdb":
                continue
            country = _text(doc_id.get("country"))
            number = _text(doc_id.get("doc-number"))
            kind = _text(doc_id.get("kind"))
            if country and number:
                return f"{country}{number}{kind}"
    return None


def _title(doc: dict) -> str | None:
    """Prefer the English title; fall back to whatever language is present —
    a TR-only publication with no English title is still worth surfacing."""
    titles = _as_list((doc.get("bibliographic-data") or {}).get("invention-title"))
    fallback = None
    for entry in titles:
        text = _text(entry)
        if not text:
            continue
        if entry.get("@lang") == "en":
            return text
        fallback = fallback or text
    return fallback


def _abstract(doc: dict) -> str | None:
    for entry in _as_list(doc.get("abstract")):
        parts = [_text(p) for p in _as_list(entry.get("p"))]
        text = " ".join(p for p in parts if p).strip()
        if text and (entry.get("@lang") == "en" or len(_as_list(doc.get("abstract"))) == 1):
            return text
    return None


def _inventors(doc: dict) -> list[str]:
    """Inventor names, de-duplicated. OPS repeats each party once per name
    format (`epodoc` and `original`), which would otherwise double every
    inventor in the list."""
    parties = (doc.get("bibliographic-data") or {}).get("parties") or {}
    out: list[str] = []
    seen: set[str] = set()
    for inventor in _as_list(parties.get("inventors", {}).get("inventor")):
        name = _text(inventor.get("inventor-name", {}).get("name"))
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def _classifications(doc: dict) -> list[str]:
    """CPC codes as `section+class+subclass+group/subgroup`, e.g. "G06N3/08"."""
    bib = doc.get("bibliographic-data") or {}
    out: list[str] = []
    for cpc in _as_list((bib.get("patent-classifications") or {}).get("patent-classification")):
        section = _text(cpc.get("section"))
        klass = _text(cpc.get("class"))
        subclass = _text(cpc.get("subclass"))
        main_group = _text(cpc.get("main-group"))
        subgroup = _text(cpc.get("subgroup"))
        if not (section and klass):
            continue
        code = f"{section}{klass}{subclass}"
        if main_group:
            code = f"{code}{main_group}/{subgroup}" if subgroup else f"{code}{main_group}"
        if code not in out:
            out.append(code)
    return out[:_MAX_CATEGORIES]


def _published_at(doc: dict) -> datetime | None:
    """`date` is YYYYMMDD on the docdb publication reference."""
    for ref in _as_list((doc.get("bibliographic-data") or {}).get("publication-reference")):
        for doc_id in _as_list(ref.get("document-id")):
            raw = _text(doc_id.get("date"))
            if len(raw) == 8 and raw.isdigit():
                try:
                    return datetime(int(raw[:4]), int(raw[4:6]), int(raw[6:8]), tzinfo=UTC)
                except ValueError:
                    continue
    return None


def _to_payload(doc: dict) -> PaperPayload | None:
    publication_number = _publication_number(doc)
    if not publication_number:
        return None
    title = _title(doc)
    if not title:
        return None
    return PaperPayload(
        source=SOURCE_NAME,
        external_id=publication_number,
        title=title,
        abstract=_abstract(doc),
        authors=_inventors(doc),
        url=_ESPACENET_URL.format(pn=publication_number),
        pdf_url=None,
        published_at=_published_at(doc),
        categories=_classifications(doc),
        kind="patent",
        # Patents have no DOI. Leaving this None keeps them out of the
        # DOI-first branch of `upsert_paper`, so they dedup purely on
        # (source, external_id) — which is what a publication number is.
        doi=None,
    )


def _documents(payload: dict) -> list[dict]:
    """Dig the publication list out of OPS's deeply nested envelope."""
    biblio = (
        (payload or {})
        .get("ops:world-patent-data", {})
        .get("ops:biblio-search", {})
        .get("ops:search-result", {})
    )
    return _as_list(biblio.get("exchange-documents"))


def _search_request(cql: str, max_results: int) -> requests.Response:
    """One authenticated biblio search, retrying once on a 401.

    A cached token can expire between the freshness check and the request
    landing at EPO. Retrying once with a forced refresh is simpler and more
    truthful than widening the skew margin until the race "cannot" happen.
    """
    params = {"q": cql, "Range": f"1-{min(max_results, _PAGE_SIZE)}"}
    resp = requests.get(
        _SEARCH_URL,
        params=params,
        headers={"Authorization": f"Bearer {_token()}", "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    if resp.status_code == 401:
        logger.info("epo_ops_token_rejected_retrying")
        resp = requests.get(
            _SEARCH_URL,
            params=params,
            headers={
                "Authorization": f"Bearer {_token(force_refresh=True)}",
                "Accept": "application/json",
            },
            timeout=_TIMEOUT,
        )
    return resp


def _charge_bytes(resp: requests.Response) -> None:
    """Settle the real response size against the weekly byte budget.

    Deliberately does not raise when the budget is now over: the bytes have
    already been spent, and failing this call would discard results we paid
    for. The *next* call is the one that gets refused.
    """
    try:
        size = len(resp.content or b"")
    except Exception:  # noqa: BLE001 — a size we cannot read is not a failure
        return
    consume_quota(SOURCE_NAME, cost=0, bytes_=max(0, size - _NOMINAL_BYTES))


def search(query: str, *, max_results: int = 25) -> list[PaperPayload]:
    """One CQL search. `query` is raw CQL — `search_for_keywords` builds it.

    Raises `SourceThrottledError` when either the per-second slot or the
    weekly byte budget refuses, rather than returning `[]`: an empty list
    would be recorded as a successful scan with no results and shown to the
    user as "nothing out there" (see `ratelimit.SourceThrottledError`).
    """
    query = (query or "").strip()
    if not query:
        return []
    if not credentials_ok():
        raise SourceThrottledError("epo_ops credentials missing")
    if not epo_ops_slot():
        logger.warning("epo_ops_rate_limited")
        raise SourceThrottledError("epo_ops rate limit")
    # Reserve a nominal amount before the call; `_charge_bytes` settles up
    # once the true size is known.
    if not consume_quota(SOURCE_NAME, cost=1, bytes_=_NOMINAL_BYTES):
        logger.warning("epo_ops_quota_exhausted")
        raise SourceThrottledError("epo_ops weekly quota exhausted")

    resp = _search_request(query, max_results)
    _charge_bytes(resp)
    if resp.status_code == 404:
        # OPS answers 404 for "your query matched nothing", which is a real
        # (and common) result, not a transport failure.
        logger.info("epo_ops_search_empty", query=query)
        return []
    resp.raise_for_status()

    docs = _documents(resp.json())
    out = [p for p in (_to_payload(d) for d in docs) if p is not None]
    logger.info("epo_ops_search_done", query=query, hits=len(out))
    return out[:max_results]


def search_for_keywords(keywords: list[str], *, max_results: int = 25) -> list[PaperPayload]:
    """All keywords in **one** request — CQL's `or` does the work that
    `semantic_scholar`/`crossref` need a request per keyword for.

    Consequence worth stating: there is no partial success to salvage here. If
    the single request fails, the failure propagates, and `scrape_for_user`
    records the source with its -1 sentinel — which is the honest outcome.
    """
    cql = build_cql([kw for kw in keywords if kw and kw.strip()])
    if not cql:
        return []
    return search(cql, max_results=min(max_results, _MAX_RESULTS))
