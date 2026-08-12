"""Shared payload shape every source adapter returns.

One dataclass for all sources so the persistence layer (service.upsert_paper)
never needs to know which API a paper came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PaperPayload:
    source: str
    external_id: str  # source-local id, e.g. "2401.12345v2" / S2 paperId / PMID
    title: str
    abstract: str | None
    authors: list[str]
    url: str | None
    pdf_url: str | None
    published_at: Any  # datetime.datetime, kept as Any to avoid tight coupling
    categories: list[str]
    # "news" for RSS/industry-announcement sources; None ("paper") for the
    # academic search adapters (arxiv/semantic_scholar/pubmed) — kept
    # optional with a default so existing adapters don't need to change.
    kind: str | None = None
    doi: str | None = None
    # Journal quality signals (Faz 5.3). Only OpenAlex and Crossref populate
    # these; every other adapter leaves them None and picks the values up for
    # free when a DOI match enriches the shared row. Defaulted for the same
    # reason `kind` is: an existing adapter must not have to change to keep
    # working.
    issn_l: str | None = None
    cited_by_count: int | None = None

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
            "kind": self.kind,
            "doi": self.doi,
            "issn_l": self.issn_l,
            "cited_by_count": self.cited_by_count,
        }
