"""Celery tasks for open-access full text (Faz 7).

Walks papers that have a fetchable OA location and no full-text attempt yet,
fetches each one, and records what came back.

Two things this deliberately does not do.

It does not retry forever. A failure is classified as permanent or transient,
and a permanent one is written down as an attempt (`fulltext_chars = 0`) so the
paper leaves the queue. Without that, one PDF served as a scanned image would
be re-fetched every night for the life of the deployment.

It does not store text it may not store. `fulltext.fetch_fulltext` returns the
licence decision alongside the text, and the write below is the only place that
decision is acted on -- see `REDISTRIBUTABLE_LICENSES`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from app.extensions import db
from app.modules.scrape.fulltext import FullTextError, fetch_fulltext
from app.modules.scrape.models import Paper
from app.tasks import celery_app

logger = structlog.get_logger()

#: Failures worth another attempt on a later run: the far side was busy,
#: unreachable, or slow. Everything else is a property of the document and will
#: not change by asking again.
_TRANSIENT_MARKERS = (
    "request failed",
    "read failed",
    "rate limit",
    "HTTP 5",
    "HTTP 429",
    "HTTP 408",
)


def _is_transient(message: str) -> bool:
    return any(marker in message for marker in _TRANSIENT_MARKERS)


@celery_app.task(name="fulltext.fetch_for_paper")
def fetch_for_paper_task(paper_id: int) -> bool:
    """Fetch and record one paper's OA full text. True when text was obtained."""
    paper = db.session.get(Paper, paper_id)
    if paper is None or not paper.oa_url:
        return False

    try:
        result = fetch_fulltext(paper.oa_url, license_=paper.oa_license)
    except FullTextError as exc:
        message = str(exc)
        if _is_transient(message):
            # Leave `fulltext_fetched_at` NULL so the sweep picks it up again.
            logger.info("fulltext_transient_failure", paper_id=paper_id, reason=message)
            return False
        paper.fulltext_chars = 0
        paper.fulltext_fetched_at = datetime.now(UTC)
        db.session.commit()
        logger.info("fulltext_permanent_failure", paper_id=paper_id, reason=message)
        return False
    except Exception:  # noqa: BLE001 — one hostile document must not stop the sweep
        logger.exception("fulltext_unexpected_failure", paper_id=paper_id)
        return False

    paper.fulltext_chars = result.chars
    paper.fulltext_fetched_at = datetime.now(UTC)
    # The whole licence question, in one line. `storable` was decided in
    # fulltext.py against the licence OpenAlex reported; nothing here re-decides
    # it, and nothing else in the codebase writes this column.
    paper.fulltext = result.text if result.storable else None
    db.session.commit()

    logger.info(
        "fulltext_fetched",
        paper_id=paper_id,
        chars=result.chars,
        stored=result.storable,
        license=result.license,
    )
    return True


@celery_app.task(name="fulltext.fetch_pending")
def fetch_pending_task(limit: int = 25) -> int:
    """Sweep papers with an OA location and no attempt recorded yet.

    `limit` is small on purpose: each item is a network fetch plus a PDF parse,
    and the per-host slot in `robots.host_slot` means a repository hosting many
    of them paces the whole batch anyway.
    """
    pending = (
        Paper.query.filter(
            Paper.oa_url.isnot(None),
            Paper.fulltext_fetched_at.is_(None),
        )
        .order_by(Paper.created_at.desc())
        .limit(limit)
        .all()
    )

    fetched = 0
    for paper in pending:
        try:
            if fetch_for_paper_task(paper.id):
                fetched += 1
        except Exception:  # noqa: BLE001 — see above; the sweep continues
            logger.exception("fulltext_sweep_item_failed", paper_id=paper.id)

    logger.info("fulltext_sweep_done", considered=len(pending), fetched=fetched)
    return fetched
