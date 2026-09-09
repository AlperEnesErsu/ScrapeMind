"""Celery tasks for vector embeddings (Faz 5.4).

Backfills missing paper embeddings in batches and handles async embedding
generation when new papers are ingested.
"""

from __future__ import annotations

import structlog

from app.extensions import db
from app.modules.scrape.embedding_service import (
    embed_paper,
    embed_papers_batch,
    is_embedding_enabled,
)
from app.modules.scrape.models import Paper
from app.tasks import celery_app

logger = structlog.get_logger()


@celery_app.task(name="embeddings.embed_paper")
def embed_paper_task(paper_id: int) -> bool:
    """Async task to compute and persist embedding for a single paper."""
    if not is_embedding_enabled():
        return False

    paper = db.session.get(Paper, paper_id)
    if paper is None or paper.embedding is not None:
        return False

    return embed_paper(paper)


@celery_app.task(name="embeddings.embed_pending_papers")
def embed_pending_papers_task(limit: int = 50) -> int:
    """Batch task to backfill embeddings for papers that lack one.

    Processes up to `limit` papers per invocation to respect provider rate
    limits and avoid long-running task locks.
    """
    if not is_embedding_enabled():
        return 0

    pending = (
        Paper.query.filter(Paper.embedding.is_(None))
        .order_by(Paper.created_at.desc())
        .limit(limit)
        .all()
    )

    if not pending:
        return 0

    embedded_count = embed_papers_batch(pending)
    logger.info("embedded_pending_papers", count=embedded_count, total_pending=len(pending))
    return embedded_count
