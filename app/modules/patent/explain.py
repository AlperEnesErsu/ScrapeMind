"""Plain-language claim readings, cached (Faz 8.6).

The prompt lives in `ai_service` with every other prompt; this module decides
what goes into it and what gets reused. Readings are shared across users --
the claim text is public and identical for everyone -- and removed by cascade
when a re-ingest replaces the claim they describe.
"""

from __future__ import annotations

import structlog

from app.extensions import db
from app.modules.patent.models import PatentClaim, PatentClaimExplanation

logger = structlog.get_logger()

#: The prompt and the product are Turkish-first today, like the novelty
#: assessment and paper translation. A column rather than a constant so adding
#: English is rows, not a migration.
TARGET_LANG = "tr"
#: A claim chain deeper than this is almost always a parse artefact; the
#: independent claim and the nearest parents carry the meaning.
MAX_CHAIN = 5


def ancestor_chain(claim: PatentClaim) -> list[PatentClaim]:
    """Parents of `claim`, independent claim first, direct parent last.

    Defensive the same way `service.claim_tree` is: a parent that is missing
    ends the chain instead of raising, and a cycle stops at the first repeat
    instead of looping.
    """
    by_number = {
        c.number: c
        for c in PatentClaim.query.filter_by(patent_document_id=claim.patent_document_id).all()
    }
    chain: list[PatentClaim] = []
    seen = {claim.number}
    parent_number = claim.depends_on
    while parent_number is not None and len(chain) < MAX_CHAIN:
        parent = by_number.get(parent_number)
        if parent is None or parent.number in seen:
            break
        chain.append(parent)
        seen.add(parent.number)
        parent_number = parent.depends_on
    chain.reverse()
    return chain


def cached(claim: PatentClaim) -> PatentClaimExplanation | None:
    return PatentClaimExplanation.query.filter_by(
        patent_claim_id=claim.id, target_lang=TARGET_LANG
    ).first()


def get_or_generate(
    claim: PatentClaim, *, user=None, force: bool = False
) -> PatentClaimExplanation | None:
    """Return the cached reading, generating it on a miss or when forced.

    A failed generation does not overwrite an existing reading: `force` asks
    for a better answer, and losing the old one to a provider hiccup would
    leave the user with less than they had.
    """
    from app.modules.scrape.ai_service import _model_label, explain_patent_claim

    existing = cached(claim)
    if existing is not None and not force:
        return existing

    result = explain_patent_claim(claim, ancestor_chain(claim), user=user)
    if result is None:
        logger.info("patent_claim_explain_failed", claim_id=claim.id, force=force)
        return existing

    row = existing or PatentClaimExplanation(patent_claim_id=claim.id, target_lang=TARGET_LANG)
    row.plain = result["plain"]
    row.narrows = result["narrows"]
    row.terms = result["terms"]
    row.model_version = _model_label(user)[:64]
    db.session.add(row)
    db.session.commit()
    return row
