"""`Paper` upsert/enrichment and per-user paper links."""

from __future__ import annotations

from dataclasses import replace

import structlog
from flask_babel import gettext as _

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.doi import normalize_doi
from app.modules.scrape.models import (
    Paper,
    UserPaper,
)
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()


#: Fields that are safe to backfill on a matched row. Deliberately excludes
#: `title`, `source`, `external_id`, `kind` — those identify the row (or, for
#: title, are never blank in practice) and overwriting them on a match would
#: make upsert_paper silently rewrite identity rather than fill gaps.
_ENRICHABLE_FIELDS = (
    "abstract",
    "pdf_url",
    "url",
    "doi",
    "published_at",
    "categories",
    "authors",
    # A paper's journal does not change; only OpenAlex/Crossref report it, so
    # every other source picks it up for free through a DOI match.
    "issn_l",
    # Open access (Faz 7). Only OpenAlex reports these, so a paper first seen
    # via arXiv or PubMed gains them when a DOI match brings OpenAlex's record
    # in -- the same way issn_l arrives. Fill-only, like everything here: a
    # licence that has already been recorded is not re-decided by a later
    # source that happens to describe the same work.
    "oa_status",
    "oa_license",
    "oa_url",
)


def _is_empty(value: object) -> bool:
    """True for the "nothing here yet" shapes of our enrichable fields:
    None, empty string, empty list. Deliberately NOT true for falsy-but-
    meaningful values like 0 or False — none of the enrichable fields are
    numeric/boolean today, but this keeps the helper honest if one ever is.
    """
    if value is None:
        return True
    if isinstance(value, str | list | tuple):
        return len(value) == 0
    return False


#: Fields that are *refreshed* on every match rather than filled once.
#:
#: `cited_by_count` is a running total, not a fact about the paper: a 2019
#: article picks up citations for years. Under the fill-only rule it would
#: freeze at whatever the first source happened to report — showing "12
#: citations" forever for a paper that now has 300, which is worse than
#: showing nothing, because it looks precise.
#:
#: Keep this set small and obviously time-varying. Everything else belongs in
#: `_ENRICHABLE_FIELDS`, where "we have no basis to pick a winner between two
#: non-empty values" is the right default.
_REFRESHABLE_FIELDS = ("cited_by_count",)


def _enrich(paper: Paper, data: dict) -> bool:
    """Merge `data` onto an existing `paper` row.

    Two rules, deliberately different:

      * `_ENRICHABLE_FIELDS` — **fill-only**. Write the incoming value only if
        the existing field is empty and the incoming one is not. A populated
        field is never overwritten, even by a different-but-also-non-empty
        value from another source: with both non-empty we have no basis for
        deciding which source is right, so whatever arrived first stays.
      * `_REFRESHABLE_FIELDS` — **always updated** when the incoming value is
        present, because the field tracks something that changes over time.

    Returns whether anything actually changed, so the caller can skip a no-op
    commit.
    """
    changed = False
    for field in _ENRICHABLE_FIELDS:
        if field not in data:
            continue
        incoming = data[field]
        if _is_empty(incoming):
            continue
        if not _is_empty(getattr(paper, field)):
            continue
        setattr(paper, field, incoming)
        changed = True

    for field in _REFRESHABLE_FIELDS:
        if field not in data:
            continue
        incoming = data[field]
        # An absent count is "this source didn't say", not "zero citations" —
        # so a source that omits the field must not wipe a real number.
        if incoming is None:
            continue
        if getattr(paper, field) == incoming:
            continue
        setattr(paper, field, incoming)
        changed = True
    return changed


def upsert_paper(payload: PaperPayload | dict) -> Paper:
    """Insert a paper if we haven't seen it before; otherwise return the
    existing row, enriched with anything new the payload can fill in.

    Resolution order: normalized DOI, then `(source, external_id)`. The DOI
    is normalized on write (see `doi.normalize_doi`) so the stored form is
    always canonical and lookup is a plain `filter_by(doi=...)` rather than
    an `ilike` scan. The earlier version stored whatever string a source
    handed it and matched with `ilike`, which is case-insensitive but not
    prefix-insensitive — so "https://doi.org/10.X/Y" from one source and
    "10.X/Y" from another still created two rows.

    Enrichment is fill-only (see `_enrich`): a matched row's empty fields
    (abstract, pdf_url, url, doi, published_at, categories, authors) are
    backfilled from the new payload, but a populated field is never
    overwritten. This is deliberately conservative — we have no basis for
    picking "which source is right" when both already have a value, so we
    just keep whatever was there first.

    Edge case worth documenting: if the incoming DOI matches one existing
    row but the incoming (source, external_id) matches a *different* row
    (e.g. a stale/wrong DOI was recorded on that second row previously), the
    DOI match wins and is the one enriched — the other row is left
    untouched. Merging the two rows (e.g. moving UserPaper links across) is
    out of scope here; it would need a real migration, not an upsert.

    Race safety: the SELECT above and the INSERT below are not atomic, so two
    workers can both miss the SELECT for the same brand-new (source,
    external_id) — e.g. the same arXiv paper matching two different users'
    keywords in concurrent nightly scans — and both fall through to insert.
    Without `ON CONFLICT`, the loser's plain INSERT would raise
    `IntegrityError` against `uq_paper_source_external` and force Celery's
    retry machinery to clean up noise that was never a real failure. Instead
    the loser's insert becomes a no-op and it re-selects the winner's row
    (then enriches it with anything its own payload had that the winner's
    didn't) — same fill-only semantics as the plain-existing-row path above,
    just reached one step later.
    """
    data = payload.as_dict() if isinstance(payload, PaperPayload) else dict(payload)
    if "doi" in data:
        data["doi"] = normalize_doi(data["doi"])

    existing = None
    doi = data.get("doi")
    if doi:
        existing = Paper.query.filter_by(doi=doi).first()
    if existing is None:
        existing = Paper.query.filter_by(
            source=data["source"], external_id=data["external_id"]
        ).first()

    if existing is not None:
        if _enrich(existing, data):
            db.session.commit()
        return existing

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(Paper.__table__)
        .values(**data)
        .on_conflict_do_nothing(index_elements=["source", "external_id"])
    )
    db.session.execute(stmt)
    db.session.commit()

    paper = Paper.query.filter_by(source=data["source"], external_id=data["external_id"]).first()
    if paper is None:  # pragma: no cover — the unique index guarantees a row either way
        raise RuntimeError("upsert_paper: insert conflicted but no row was found")
    if _enrich(paper, data):
        db.session.commit()
    return paper


def link_user_paper(
    user: User, paper: Paper, *, matched_keyword: str | None
) -> tuple[UserPaper, bool]:
    """Idempotent. Returns (link, created) — created=True only on first insert."""
    existing = UserPaper.query.filter_by(user_id=user.id, paper_id=paper.id).first()
    if existing is not None:
        return existing, False
    link = UserPaper(user_id=user.id, paper_id=paper.id, matched_keyword=matched_keyword)
    db.session.add(link)
    db.session.commit()
    return link, True


def add_paper_from_url(user: User, url: str) -> tuple[UserPaper, bool]:
    """Manually add a paper from a public web URL using net_guard + web_reach.
    Returns (UserPaper link, created boolean). Raises ValueError on validation/net_guard error.
    """
    clean_url = (url or "").strip()
    if not clean_url.startswith(("http://", "https://")):
        raise ValueError(_("Invalid URL. Must start with http:// or https://"))

    allow_private = False
    try:
        from flask import current_app

        allow_private = bool(current_app.config.get("FEED_ALLOW_PRIVATE_HOSTS", False))
    except Exception:  # noqa: BLE001
        pass

    from app.modules.scrape.net_guard import is_public_http_url

    ok, _err = is_public_http_url(clean_url, allow_private=allow_private)
    if not ok:
        raise ValueError(_("URL blocked by security guard (private or loopback IP)"))

    from app.modules.scrape.sources.external_sources import search_web

    payloads = search_web(clean_url)
    if not payloads:
        raise ValueError(_("Failed to fetch web content from URL"))

    payload = replace(payloads[0], source="manual", kind="link", categories=["manual"])

    # The cleaned reader text still opens on page chrome (language switchers,
    # badges, install banners) that no regex can reliably tell from content,
    # so ask the LLM for a real summary. Best-effort: a failure here must not
    # cost the user the link, so we keep the cleaned text on any error.
    if payload.abstract:
        try:
            from app.modules.scrape.ai_service import summarize_web_content

            summary = summarize_web_content(payload.title, payload.abstract, user=user)
            if summary:
                payload = replace(payload, abstract=summary)
        except Exception:  # noqa: BLE001 — summary is a nicety, the link is the point
            logger.warning("manual_link_summary_failed", url=clean_url, user_id=user.id)

    paper = upsert_paper(payload)
    # `matched_keyword` is a stored column, so a translated label would freeze
    # the row into whichever language was active at insert time. Manually
    # added rows are identified by `source == "manual"` and labelled at render.
    link, created = link_user_paper(user, paper, matched_keyword=None)
    return link, created
