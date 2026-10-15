"""Keyword-driven scraping across the enabled sources."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import requests
import structlog

from app.core.models.user import User
from app.extensions import db
from app.modules.academic.service import list_user_keywords
from app.modules.scrape.service.papers import link_user_paper, upsert_paper
from app.modules.scrape.service.source_prefs import user_enabled_sources

logger = structlog.get_logger()


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
