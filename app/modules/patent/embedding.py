"""Claim 1 vectors for semantic patent search (Faz 8.5).

One vector per patent, from its first claim. The claim is the patent's legal
scope, and it is what a "does anyone own this idea" query is really being
matched against; the description is left to full-text search. That is also
not the chunk-and-average that Faz 7.0 rejected for papers -- nothing is split
or averaged, one legally defined unit is embedded whole.

Embedding calls are billable, so this runs as its own task on the `llm`
queue rather than inside the weekly load on `io`, and it is written to be
safe to run any number of times: it only ever looks at documents whose
claim 1 has no vector *from the current model*.
"""

from __future__ import annotations

import structlog
from flask import current_app

from app.extensions import db
from app.modules.patent.models import PatentChunk, PatentClaim, PatentDocument

logger = structlog.get_logger()

KIND_CLAIM1 = "claim1"
#: Same batch size as the paper embedder: one provider call per batch.
BATCH_SIZE = 32


def current_model() -> str:
    """ "model@dimension" -- what a stored vector must match to be comparable.

    Delegates to the paper embedder so patent chunks and papers can never
    disagree about what the current model is called.
    """
    from app.modules.scrape.embedding_service import current_embedding_model

    return current_embedding_model()


def is_enabled() -> bool:
    """Off when the deployment asked for full text only, or has no provider.

    Checked without a user on purpose: the corpus is embedded by a background
    task with the deployment's key, never with whichever user happened to
    trigger something.
    """
    if current_app.config.get("PATENT_FTS_ONLY"):
        return False
    from app.modules.scrape.embedding_service import is_embedding_enabled

    return is_embedding_enabled(user=None)


def _scope_claim(document_id: int) -> PatentClaim | None:
    """Claim 1, or the first independent claim when a reissue renumbered."""
    claims = (
        PatentClaim.query.filter(PatentClaim.patent_document_id == document_id)
        .order_by(PatentClaim.number)
        .all()
    )
    for claim in claims:
        if claim.number == 1:
            return claim
    return next((c for c in claims if c.is_independent), None)


def _pending_query():
    """Live documents with no claim-1 vector from the current model."""
    model = current_model()
    has_current = db.exists().where(
        PatentChunk.patent_document_id == PatentDocument.id,
        PatentChunk.kind == KIND_CLAIM1,
        PatentChunk.embedding.is_not(None),
        PatentChunk.embedding_model == model,
    )
    return PatentDocument.query.filter(PatentDocument.deleted_at.is_(None), ~has_current)


def pending_documents(limit: int | None = None) -> list[PatentDocument]:
    query = _pending_query().order_by(PatentDocument.grant_date.desc(), PatentDocument.id.desc())
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def embed_pending(limit: int | None = None) -> dict:
    """Embed what is missing or stale. Returns counts for the task result.

    A document whose provider call fails is left pending rather than marked
    done, so the next run tries it again; the provider is fail-open and a
    transient error must not freeze a patent out of semantic search.
    """
    if not is_enabled():
        return {"status": "skipped", "reason": "disabled", "embedded": 0}

    from app.modules.scrape.embedding_service import get_embeddings_batch

    model = current_model()
    documents = pending_documents(limit)
    embedded = failed = no_claim = 0

    for start in range(0, len(documents), BATCH_SIZE):
        batch = documents[start : start + BATCH_SIZE]
        pairs = []
        for doc in batch:
            claim = _scope_claim(doc.id)
            if claim is None or not (claim.text or "").strip():
                no_claim += 1
                continue
            pairs.append((doc, claim))
        if not pairs:
            continue

        vectors = get_embeddings_batch([c.text for _, c in pairs], user=None)
        for (doc, claim), vector in zip(pairs, vectors, strict=True):
            if vector is None:
                failed += 1
                continue
            chunk = PatentChunk.query.filter_by(
                patent_document_id=doc.id, kind=KIND_CLAIM1
            ).first() or PatentChunk(patent_document_id=doc.id, kind=KIND_CLAIM1)
            chunk.ref = str(claim.number)
            chunk.text = claim.text
            chunk.embedding = vector
            chunk.embedding_model = model
            db.session.add(chunk)
            embedded += 1
        db.session.commit()

    logger.info(
        "patent_embed_done", embedded=embedded, failed=failed, no_claim=no_claim, model=model
    )
    return {
        "status": "ok" if not failed else "partial",
        "embedded": embedded,
        "failed": failed,
        "no_claim": no_claim,
        "model": model,
    }


def coverage() -> tuple[int, int]:
    """(documents with a current-model claim-1 vector, live documents)."""
    total = PatentDocument.query.filter(PatentDocument.deleted_at.is_(None)).count()
    return total - _pending_query().count(), total
