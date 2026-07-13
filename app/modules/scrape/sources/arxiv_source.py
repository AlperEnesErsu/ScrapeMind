"""arXiv API adapter.

Thin wrapper around the `arxiv` PyPI client. Exposes the common source
interface:

  SOURCE_NAME
  search(query, max_results) -> list[PaperPayload]
  search_for_keywords(keywords, max_results) -> list[PaperPayload]

Pure I/O — no Celery, no DB, no Flask. The scrape service above this layer
handles dedupe + persistence.
"""

from __future__ import annotations

import arxiv
import structlog

from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "arxiv"


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


def search_for_keywords(keywords: list[str], *, max_results: int = 25) -> list[PaperPayload]:
    """Common source interface: one OR-combined full-text query per run.

    arXiv supports boolean expressions natively, so all keywords fit in a
    single request (the client also enforces a 3s inter-request delay —
    one call is the cheap path).
    """
    keywords = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not keywords:
        return []
    query = " OR ".join(f'all:"{kw}"' for kw in keywords)
    return search(query, max_results=max_results)
