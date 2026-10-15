"""Patent source selection and scraping."""

from __future__ import annotations

import structlog

from app.core.models.user import User
from app.modules.academic.service import list_user_keywords
from app.modules.scrape.service.papers import link_user_paper, upsert_paper
from app.modules.scrape.service.scraping import (
    _match_keyword,
    ensure_keyword_translations,
    keyword_search_terms,
)
from app.modules.scrape.service.source_prefs import admin_opted_in, effective_source_prefs
from app.modules.scrape.sources import (
    SOURCE_META,
    admin_optin_key,
    enabled_sources,
)

logger = structlog.get_logger()


def patent_sources(user: User | None = None) -> dict:
    """The deployment's usable patent sources, optionally narrowed to one
    user's preferences.

    "Usable" already means credentials are configured — `enabled_sources()`
    drops key-gated sources without them — and, when a user is given, that the
    admin has opted in and the user has not switched the source off.
    """
    available = {
        name: mod
        for name, mod in enabled_sources().items()
        if SOURCE_META.get(name, {}).get("category") == "patent"
    }
    if user is None:
        return available
    prefs = effective_source_prefs(user)
    return {name: mod for name, mod in available.items() if prefs.get(name)}


def patent_sources_available() -> bool:
    """Whether any patent source could run at all — keys present *and* the
    admin opt-in on.

    Read once by `patents.ingest_for_all_users` so a deployment that never
    configured EPO/PatentsView pays one registry lookup for the nightly
    schedule entry instead of a fan-out per user.
    """
    available = patent_sources()
    if not available:
        return False
    return any(
        admin_optin_key(name) is None or admin_opted_in(admin_optin_key(name)) for name in available
    )


def scrape_patents_for_user(user: User, *, max_results: int = 25) -> dict:
    """`scrape_for_user`, restricted to the patent sources.

    Deliberately a separate run rather than extra entries in the academic
    scan: patent sources are metered against a weekly budget, so an exhausted
    quota must not be able to mark a user's *academic* scan `partial` and
    drain the status line of meaning.

    Keyword handling is otherwise identical, including the English expansion —
    EPO's CQL and PatentsView's `_text_any` both search English text, so a
    user following "kalp yetmezliği" needs the same translation the academic
    sources get.
    """
    keyword_rows = list_user_keywords(user)
    if not keyword_rows:
        logger.info("patent_scrape_skip_no_keywords", user_id=user.id)
        return {"hits": 0, "linked": 0, "reason": "no_keywords"}

    sources = patent_sources(user)
    if not sources:
        logger.info("patent_scrape_skip_no_sources", user_id=user.id)
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
            payloads = source.search_for_keywords(terms, max_results=max_results)
        except Exception:  # noqa: BLE001 — one failing source must not kill the run
            logger.exception("patent_source_failed", source=name, user_id=user.id)
            per_source[name] = -1  # sentinel → apply_scan_result marks "partial"
            continue
        per_source[name] = len(payloads)
        hits += len(payloads)
        for payload in payloads:
            paper = upsert_paper(payload)
            _, created = link_user_paper(
                user, paper, matched_keyword=_match_keyword(paper.title, terms, alias)
            )
            if created:
                linked += 1
    logger.info("patent_scrape_done", user_id=user.id, hits=hits, linked=linked)
    return {"hits": hits, "linked": linked, "sources": per_source}


def search_patents_live(keywords: list[str], *, max_results: int = 20) -> tuple[list, dict]:
    """Query the patent sources directly and return payloads **without
    persisting them** — the prior-art search page (Faz 5.2).

    One-off prior-art queries must not pollute the `papers` table: a user
    checking twenty variations of an invention idea would otherwise leave
    hundreds of rows nobody's feed ever wanted, and EPO's fair-use terms are
    happier with a search that keeps nothing. Only the nightly keyword scan
    (`scrape_patents_for_user`) persists.

    Returns `(payloads, per_source)` where `per_source` uses the same -1
    sentinel as the scan paths so the page can say "EPO was rate limited"
    rather than silently showing fewer results.
    """
    terms = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not terms:
        return [], {}

    out: list = []
    per_source: dict[str, int] = {}
    for name, source in patent_sources().items():
        try:
            payloads = source.search_for_keywords(terms, max_results=max_results)
        except Exception:  # noqa: BLE001 — report the failure, keep the others
            logger.exception("patent_live_search_failed", source=name)
            per_source[name] = -1
            continue
        per_source[name] = len(payloads)
        out.extend(payloads)

    # Newest first: for prior art, a recent publication is the one most likely
    # to matter, and it also puts the "is this already claimed" answer on top.
    out.sort(key=lambda p: (p.published_at is not None, p.published_at), reverse=True)
    return out[:max_results], per_source
