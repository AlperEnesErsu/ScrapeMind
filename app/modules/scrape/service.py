"""Persistence layer for scraped papers.

The service deduplicates incoming payloads against (source, external_id)
and keeps a per-user record of which keyword surfaced what — that's how
the "For you" dashboard becomes a personal feed instead of a firehose.
"""

from __future__ import annotations

import structlog
from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db
from app.modules.academic.service import list_user_identifiers, list_user_keywords
from app.modules.scrape.models import Paper, UserPaper
from app.modules.scrape.sources.arxiv_source import PaperPayload
from app.modules.scrape.sources.arxiv_source import search as arxiv_search

logger = structlog.get_logger()


def upsert_paper(payload: PaperPayload | dict) -> Paper:
    """Insert a paper if we haven't seen it before, return the row either way."""
    data = payload.as_dict() if isinstance(payload, PaperPayload) else dict(payload)
    existing = Paper.query.filter_by(source=data["source"], external_id=data["external_id"]).first()
    if existing is not None:
        return existing
    paper = Paper(**data)
    db.session.add(paper)
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


def _build_arxiv_query(keywords: list[str], orcid_values: list[str]) -> str:
    """Compose an arXiv search expression from user interests + identifiers."""
    parts: list[str] = []
    # Keywords go into the full-text search ("all:")
    for kw in keywords:
        parts.append(f'all:"{kw}"')
    # ORCID isn't supported by arXiv search directly; fall back to nothing for
    # now. (Later slices might match by author name strings from user_identifiers.)
    _ = orcid_values  # placeholder until author-name identifiers land
    return " OR ".join(parts)


def scrape_arxiv_for_user(user: User, *, max_results: int = 25) -> dict:
    """Run an arXiv search using this user's keywords; persist + link the
    results back to them. Returns a summary dict for the calling task."""
    keywords = [kw.value for kw in list_user_keywords(user)]
    orcid_idents = [
        i.value for i in list_user_identifiers(user, type_code="orcid") if i.is_verified
    ]
    if not keywords:
        logger.info("scrape_skip_no_keywords", user_id=user.id)
        return {"hits": 0, "linked": 0, "reason": "no_keywords"}

    query = _build_arxiv_query(keywords, orcid_idents)
    payloads = arxiv_search(query, max_results=max_results)

    linked = 0
    for payload in payloads:
        paper = upsert_paper(payload)
        # Best-effort keyword match: pick the first keyword whose tokens all
        # appear in the title — good enough for analytics, perfect for v1.
        matched_kw = next(
            (kw for kw in keywords if all(tok in paper.title.lower() for tok in kw.split())),
            keywords[0],
        )
        _, created = link_user_paper(user, paper, matched_keyword=matched_kw)
        if created:
            linked += 1
    logger.info("scrape_done", user_id=user.id, hits=len(payloads), linked=linked)
    return {"hits": len(payloads), "linked": linked, "query": query}


def list_user_papers(user: User, *, limit: int = 50) -> list[UserPaper]:
    return (
        UserPaper.query.filter_by(user_id=user.id)
        .join(Paper)
        .order_by(desc(Paper.published_at), desc(UserPaper.created_at))
        .limit(limit)
        .all()
    )
