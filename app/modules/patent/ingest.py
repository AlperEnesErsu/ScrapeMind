"""Turning one weekly USPTO file into the rolling window.

The pipeline is deliberately ordered *filter first, write second*. A weekly
grant file holds 6,000-8,000 patents and the CPC rule keeps a few hundred;
parsing everything is unavoidable, but writing everything and deleting it
afterwards would multiply the database work by roughly twenty for no gain.

Deduplication is by `doc_number` and is not the fill-only merge `papers` uses
(`docs/SCRAPING.md` §8). There, several sources compete to describe one paper
and no source is authoritative. Here USPTO is the only source, so a re-parse
is simply newer truth — guarded by `raw_sha256` so an unchanged document is
skipped rather than rewritten.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import structlog
from flask import current_app

from app.extensions import db
from app.modules.patent import classify, parser, service, uspto
from app.modules.patent.models import PatentClaim, PatentDocument, PatentIngestRun

logger = structlog.get_logger()

#: Rows are flushed in batches so one weekly file is not a single enormous
#: transaction, and a crash leaves a partial-but-consistent window rather
#: than nothing.
_BATCH = 100


def _cpc_prefixes() -> tuple[tuple[str, ...], tuple[str, ...]]:
    def split(value: str | None) -> tuple[str, ...]:
        return tuple(p.strip().upper() for p in (value or "").split(",") if p.strip())

    core = split(current_app.config.get("PATENT_AI_CPC_CODES")) or classify.CORE_DEFAULT
    extended = split(current_app.config.get("PATENT_AI_CPC_EXTENDED"))
    return core, extended


def _replace_claims(document: PatentDocument, parsed: parser.ParsedPatent) -> None:
    """Claims are replaced wholesale, never merged.

    A re-issued or corrected grant can renumber claims, and merging by number
    would leave a tree stitched together from two different versions of the
    document — worse than either version alone.
    """
    PatentClaim.query.filter_by(patent_document_id=document.id).delete()
    for claim in parsed.claims:
        db.session.add(
            PatentClaim(
                patent_document_id=document.id,
                number=claim.number,
                is_independent=claim.is_independent,
                depends_on=claim.depends_on,
                text=claim.text,
            )
        )


def upsert(parsed: parser.ParsedPatent, *, ai_source: str, source_file: str) -> bool:
    """Insert or refresh one patent. Returns True if anything was written."""
    existing = PatentDocument.query.filter_by(doc_number=parsed.doc_number).first()

    if existing is not None and existing.raw_sha256 == parsed.raw_sha256:
        return False

    document = existing or PatentDocument(doc_number=parsed.doc_number)
    document.country = parsed.country
    document.kind_code = parsed.kind_code
    document.title = parsed.title
    document.abstract = parsed.abstract
    document.description = parsed.description
    document.filing_date = parsed.filing_date
    document.grant_date = parsed.grant_date
    document.priority_date = parsed.priority_date
    document.assignees = parsed.assignees
    document.inventors = parsed.inventors
    document.cpc_codes = parsed.cpc_codes
    document.ai_source = ai_source
    document.claim_count = parsed.claim_count
    document.source_file = source_file
    document.raw_sha256 = parsed.raw_sha256
    document.ingested_at = datetime.now(UTC)

    if existing is None:
        db.session.add(document)
    db.session.flush()

    _replace_claims(document, parsed)
    return True


def ingest_file(
    xml_path: str, *, source_file: str | None = None, limit: int | None = None
) -> PatentIngestRun:
    """Stream one weekly file into the window, recording the run.

    The run row is written before the work starts and closed in a `finally`,
    so a crashed load leaves an `error` row rather than a `running` one that
    nothing will ever resolve.
    """
    name = source_file or xml_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    run = PatentIngestRun(source_file=name, status="running")
    db.session.add(run)
    db.session.commit()

    core, extended = _cpc_prefixes()
    seen = kept = 0

    try:
        for parsed in parser.iter_patents(xml_path):
            seen += 1
            ai_source = classify.classify(parsed.cpc_codes, core=core, extended=extended)
            if ai_source is None:
                continue
            if upsert(parsed, ai_source=ai_source, source_file=name):
                kept += 1
            if kept and kept % _BATCH == 0:
                db.session.commit()
            if limit is not None and kept >= limit:
                break
        db.session.commit()
        run.status = "ok"
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        db.session.rollback()
        run = db.session.get(PatentIngestRun, run.id)
        run.status = "error"
        run.error = str(exc)[:2000]
        raise
    finally:
        run.documents_seen = seen
        run.documents_kept = kept
        run.finished_at = datetime.now(UTC)
        db.session.commit()
        logger.info("patent_ingest_done", file=name, seen=seen, kept=kept, status=run.status)

    return run


def purge_window(now: date | None = None) -> int:
    """Drop documents that have fallen out of the window. Returns the count.

    Claims and chunks go with them through `ON DELETE CASCADE`. What does not
    go is anything a user kept: `patent_documents.paper_id` is `SET NULL`, so
    a library row survives its corpus copy being purged.
    """
    cutoff = service.window_start(now)
    doomed = PatentDocument.query.filter(PatentDocument.grant_date < cutoff)
    count = doomed.count()
    if count:
        doomed.delete(synchronize_session=False)
        db.session.commit()
    logger.info("patent_purge_done", cutoff=cutoff.isoformat(), removed=count)
    return count


def refresh_window(*, today: date | None = None, limit: int | None = None) -> dict:
    """Discover, download, ingest, purge — the whole weekly cycle."""
    weekly = uspto.discover_latest(today=today)
    archive, _sha = uspto.download(weekly)
    xml_path = uspto.ensure_xml(archive)
    run = ingest_file(xml_path, source_file=weekly.name, limit=limit)
    removed = purge_window(today)
    return {
        "file": weekly.name,
        "seen": run.documents_seen,
        "kept": run.documents_kept,
        "purged": removed,
        "status": run.status,
    }
