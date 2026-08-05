"""Source adapter registry.

Every adapter module exposes the same contract:

  SOURCE_NAME: str
  search(query, *, max_results) -> list[PaperPayload]
  search_for_keywords(keywords, *, max_results) -> list[PaperPayload]

`enabled_sources()` reads SCRAPE_SOURCES (comma-separated names) so a
deployment can trim the list — e.g. `SCRAPE_SOURCES=arxiv` to skip the
rate-limited public APIs. Unknown names are logged and ignored.
"""

from __future__ import annotations

import os
from types import ModuleType
from typing import Any

import structlog

from app.modules.scrape.sources import (
    arxiv_source,
    external_sources,
    pubmed_source,
    rss_source,
    semantic_scholar_source,
)

logger = structlog.get_logger()

AVAILABLE_SOURCES: dict[str, Any] = {
    arxiv_source.SOURCE_NAME: arxiv_source,
    semantic_scholar_source.SOURCE_NAME: semantic_scholar_source,
    pubmed_source.SOURCE_NAME: pubmed_source,
    external_sources.YOUTUBE_SOURCE_NAME: external_sources.youtube_adapter,
    external_sources.GITHUB_SOURCE_NAME: external_sources.github_adapter,
    external_sources.WEB_SOURCE_NAME: external_sources.web_adapter,
}
# Every RSS feed (app/modules/scrape/sources/rss_source.py:FEEDS) registers
# under its own key, sharing the one `rss_source` module — the module's
# `search_for_keywords` is a no-op (feeds are ingested globally, not via the
# per-user keyword-search flow) so this stays safe if a source loop ever
# hits it.
for _feed in rss_source.FEEDS:
    AVAILABLE_SOURCES[_feed["key"]] = rss_source

# Topic taxonomy (Faz 3 Bölüm A) — a small, easily-extended set of domain
# keys used to (a) tag every curated source and (b) classify a user's
# interests (see ai_service.classify_user_topics). Values are English msgids;
# templates wrap them in `_()` for translation — keep this dict flat so a new
# domain is a one-line addition.
TOPICS: dict[str, str] = {
    "ai": "Artificial Intelligence",
    "ml": "Machine Learning",
    "cs": "Computer Science",
    "physics": "Physics",
    "math": "Mathematics",
    "biomed": "Biomedicine",
    "social": "Social Sciences",
    "humanities": "Humanities",
    "general": "General",
}

# UI-facing metadata for each source. Kept here — not on the adapter modules —
# so adapters stay pure I/O. `label`/`desc` are English msgids; templates wrap
# them in `_()` for translation. `icon` is a bootstrap-icons class.
# `topics` (list of TOPICS keys) + `category` ("academic"|"feed") drive the
# interest-aware source picker (Faz 3 Bölüm C) — `category` distinguishes the
# always-on-by-default academic databases from the topic-gated RSS feeds.
SOURCE_META: dict[str, dict] = {
    "arxiv": {
        "label": "arXiv",
        "icon": "bi-file-earmark-text",
        "desc": "Physics, CS and mathematics preprints",
        "url": "https://arxiv.org",
        "topics": ["cs", "physics", "math", "general"],
        "category": "academic",
    },
    "semantic_scholar": {
        "label": "Semantic Scholar",
        "icon": "bi-diagram-3",
        "desc": "Multi-field corpus with citation graph",
        "url": "https://www.semanticscholar.org",
        "topics": ["general"],
        "category": "academic",
    },
    "pubmed": {
        "label": "PubMed",
        "icon": "bi-heart-pulse",
        "desc": "Biomedical and life sciences literature",
        "url": "https://pubmed.ncbi.nlm.nih.gov",
        "topics": ["biomed"],
        "category": "academic",
    },
    "youtube_reach": {
        "label": "YouTube Videos",
        "icon": "bi-youtube",
        "desc": "YouTube video search and metadata",
        "url": "https://youtube.com",
        "topics": ["general", "ai", "cs"],
        "category": "feed",
    },
    "github_reach": {
        "label": "GitHub Trending & Repos",
        "icon": "bi-github",
        "desc": "GitHub repository search and code trends",
        "url": "https://github.com",
        "topics": ["cs", "ai", "ml"],
        "category": "feed",
    },
    "web_reach": {
        "label": "Web Reader",
        "icon": "bi-globe2",
        "desc": "Web content and research article reader",
        "url": "https://r.jina.ai",
        "topics": ["general"],
        "category": "feed",
    },
    "manual": {
        "label": "Manual",
        "icon": "bi-link-45deg",
        "desc": "Manually added link",
        "url": "",
        "topics": ["general"],
        "category": "feed",
    },
}
for _feed in rss_source.FEEDS:
    SOURCE_META[_feed["key"]] = {
        "label": _feed["label"],
        "icon": _feed["icon"],
        "desc": _feed["desc"],
        "url": _feed["url"],
        "topics": ["ai", "ml"],
        "category": "feed",
    }

_DEFAULT = "arxiv,semantic_scholar,pubmed,youtube_reach,github_reach,web_reach," + ",".join(
    f["key"] for f in rss_source.FEEDS
)


def enabled_sources() -> dict[str, ModuleType]:
    """Registry filtered by the SCRAPE_SOURCES env var (default: all)."""
    raw = os.getenv("SCRAPE_SOURCES", _DEFAULT)
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    out: dict[str, ModuleType] = {}
    for name in names:
        mod = AVAILABLE_SOURCES.get(name)
        if mod is None:
            logger.warning("scrape_source_unknown", name=name)
            continue
        out[name] = mod
    return out


def source_options() -> list[dict]:
    """UI-ready list of the deployment's enabled sources, each merged with its
    display metadata. Unknown sources (no SOURCE_META row) fall back to the raw
    name as label so the UI never breaks on a new adapter."""
    out: list[dict] = []
    for name in enabled_sources():
        meta = SOURCE_META.get(name, {})
        out.append(
            {
                "name": name,
                "label": meta.get("label", name.replace("_", " ").title()),
                "icon": meta.get("icon", "bi-database"),
                "desc": meta.get("desc", ""),
                "url": meta.get("url", ""),
                "topics": meta.get("topics", []),
                "category": meta.get("category", "academic"),
            }
        )
    return out


def suggested_sources(user_topics: list[str]) -> set[str]:
    """Names of the deployment's enabled sources whose `topics` intersect
    `user_topics` — the "suggested for you" set used by the source picker
    grouping and by `service.user_enabled_sources`'s topic-aware default.

    A user classified only as `["general"]` does NOT get every feed marked
    suggested — none of the curated RSS feeds carry the "general" topic (see
    SOURCE_META above), so only a *real* domain overlap counts. Broad
    academic catalogs (arXiv, Semantic Scholar) do carry "general", which is
    intentional — they're relevant regardless of specialty.
    """
    topics = {t for t in (user_topics or []) if t}
    if not topics:
        return set()
    out: set[str] = set()
    for name in enabled_sources():
        meta = SOURCE_META.get(name, {})
        src_topics = set(meta.get("topics") or [])
        if src_topics & topics:
            out.add(name)
    return out
