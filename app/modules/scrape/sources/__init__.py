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

import structlog

from app.modules.scrape.sources import arxiv_source, pubmed_source, semantic_scholar_source

logger = structlog.get_logger()

AVAILABLE_SOURCES: dict[str, ModuleType] = {
    arxiv_source.SOURCE_NAME: arxiv_source,
    semantic_scholar_source.SOURCE_NAME: semantic_scholar_source,
    pubmed_source.SOURCE_NAME: pubmed_source,
}

_DEFAULT = "arxiv,semantic_scholar,pubmed"


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
