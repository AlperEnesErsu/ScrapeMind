"""arXiv API adapter.

Thin wrapper around the `arxiv` PyPI client. Exposes:

  search(query, max_results) -> list[PaperPayload]

where each PaperPayload is a plain dict ready to be diffed against the
`papers` table — no Celery, no DB, no Flask. Pure I/O. The scrape task
above this layer handles dedupe + persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import arxiv
import structlog

logger = structlog.get_logger()

SOURCE_NAME = "arxiv"


@dataclass(frozen=True)
class PaperPayload:
    source: str
    external_id: str  # e.g. "2401.12345v2"
    title: str
    abstract: str | None
    authors: list[str]
    url: str | None
    pdf_url: str | None
    published_at: Any  # datetime.datetime, kept as Any to avoid tight coupling
    categories: list[str]

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "external_id": self.external_id,
            "title": self.title,
            "abstract": self.abstract,
            "authors": self.authors,
            "url": self.url,
            "pdf_url": self.pdf_url,
            "published_at": self.published_at,
            "categories": self.categories,
        }


def _entry_external_id(entry: arxiv.Result) -> str:
    """Return the arXiv-side id we'll dedupe against.

    arxiv.Result.entry_id is a full URL like
    'http://arxiv.org/abs/2401.12345v2'; we keep just the trailing id.
    """
    return entry.entry_id.rsplit("/", 1)[-1]


def search(query: str, *, max_results: int = 25) -> list[PaperPayload]:
    """Run an arXiv search and return normalised payloads.

    Network-only — no DB or Flask required. Safe to call from a Celery worker
    or directly from a script for ad-hoc poking.
    """
    if not query or not query.strip():
        return []

    client = arxiv.Client(page_size=min(max_results, 50), delay_seconds=3.0, num_retries=3)
    search = arxiv.Search(
        query=query.strip(),
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    out: list[PaperPayload] = []
    for entry in client.results(search):
        out.append(
            PaperPayload(
                source=SOURCE_NAME,
                external_id=_entry_external_id(entry),
                title=(entry.title or "").strip().replace("\n", " "),
                abstract=(entry.summary or "").strip() or None,
                authors=[a.name for a in (entry.authors or [])],
                url=entry.entry_id,
                pdf_url=entry.pdf_url,
                published_at=entry.published,
                categories=list(entry.categories or []),
            )
        )
    logger.info("arxiv_search_done", query=query, hits=len(out))
    return out
