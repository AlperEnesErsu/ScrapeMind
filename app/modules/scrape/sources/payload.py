"""Shared payload shape every source adapter returns.

One dataclass for all sources so the persistence layer (service.upsert_paper)
never needs to know which API a paper came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class PaperPayloadError(ValueError):
    """A source adapter produced a shape `Paper(**payload.as_dict())` cannot
    accept safely.

    Raised from `PaperPayload.__post_init__`, i.e. at construction time in the
    adapter itself — not later at DB-write time, which is exactly the point:
    the message always names the offending field so the broken adapter is
    obvious from the traceback alone.
    """


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
    # Open access (Faz 7). Defaulted for the same reason `issn_l` is: an
    # existing adapter must not have to change to keep working, and a source
    # that cannot tell OA from closed should say nothing rather than guess.
    oa_status: str | None = None
    oa_license: str | None = None
    oa_url: str | None = None

    def __post_init__(self) -> None:
        """Light, structural validation only — no normalising, no coercing.

        `frozen=True` means a field can't be repaired here even if we wanted
        to (that would need `object.__setattr__`, and fixing up a bad value
        would hide exactly the adapter bug this is meant to surface). A
        source that produces something this rejects has a bug in its
        `_to_payload`; the fix belongs there, not in a looser check here.
        """
        # `source`/`external_id` identify the row for dedup (see
        # `service.upsert_paper`) — either being empty means there is nothing
        # to key on.
        if not self.source:
            raise PaperPayloadError(
                f"PaperPayload.source must not be empty (external_id={self.external_id!r})"
            )
        if not self.external_id:
            raise PaperPayloadError(
                f"PaperPayload.external_id must not be empty (source={self.source!r})"
            )
        if not isinstance(self.title, str) or not self.title.strip():
            raise PaperPayloadError(
                f"PaperPayload.title must not be empty/whitespace-only "
                f"(source={self.source!r}, external_id={self.external_id!r})"
            )
        if not isinstance(self.authors, list) or not all(isinstance(a, str) for a in self.authors):
            raise PaperPayloadError(
                f"PaperPayload.authors must be list[str], got {self.authors!r} "
                f"(source={self.source!r}, external_id={self.external_id!r})"
            )
        if not isinstance(self.categories, list):
            raise PaperPayloadError(
                f"PaperPayload.categories must be a list, got {self.categories!r} "
                f"(source={self.source!r}, external_id={self.external_id!r})"
            )
        if self.published_at is not None:
            if not isinstance(self.published_at, datetime):
                raise PaperPayloadError(
                    f"PaperPayload.published_at must be None or datetime, got "
                    f"{type(self.published_at).__name__} "
                    f"(source={self.source!r}, external_id={self.external_id!r})"
                )
            # A naive datetime silently shifts on write — the column is
            # `DateTime(timezone=True)` (see `models.Paper.published_at`) — so
            # this is rejected rather than assumed-UTC here.
            if self.published_at.tzinfo is None:
                raise PaperPayloadError(
                    f"PaperPayload.published_at must be tz-aware (got a naive datetime) "
                    f"(source={self.source!r}, external_id={self.external_id!r})"
                )
        if self.cited_by_count is not None and not isinstance(self.cited_by_count, int):
            raise PaperPayloadError(
                f"PaperPayload.cited_by_count must be None or int, got "
                f"{type(self.cited_by_count).__name__} "
                f"(source={self.source!r}, external_id={self.external_id!r})"
            )

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
