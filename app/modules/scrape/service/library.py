"""Listing, counting and searching a user's papers."""

from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy import desc

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import (
    Journal,
    Paper,
    UserPaper,
)

logger = structlog.get_logger()


def _user_papers_query(user: User, view: str):
    """Shared filter builder for list/count. View vocabulary matches
    list_user_papers below."""
    q = UserPaper.query.filter_by(user_id=user.id)
    if view == "discover":
        q = q.filter(UserPaper.dismissed_at.is_(None))
    elif view == "favorites":
        q = q.filter(UserPaper.is_favorite.is_(True), UserPaper.dismissed_at.is_(None))
    elif view == "read_later":
        q = q.filter(UserPaper.read_later.is_(True), UserPaper.dismissed_at.is_(None))
    elif view == "dismissed":
        q = q.filter(UserPaper.dismissed_at.isnot(None))
    return q


def _contains_pattern(q: str) -> str:
    """A case-folded LIKE pattern that matches `q` literally.

    Without escaping, a user typing `%` or `_` matched almost every row, and a
    search for `50%` or `snake_case` matched the wrong ones. Pair with
    `.like(pattern, escape="\\")`.

    The escaping here is the guard, not the `escape=` argument: backslash is
    already Postgres's default LIKE escape, so the argument is redundant there
    (a mutation test confirmed removing it changes nothing). It is kept so the
    pattern means the same on any engine that does not default to backslash.
    """
    escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_user_papers(
    user: User,
    *,
    limit: int = 50,
    view: str = "discover",
    q: str | None = None,
    kinds: tuple[str, ...] | None = None,
    quartiles: tuple[str, ...] | None = None,
    semantic: bool = False,
) -> list[UserPaper]:
    """List a user's surfaced papers.

    Views:
        * "discover" — feed, hides dismissed rows
        * "favorites" — starred only
        * "dismissed" — hidden bin (recovery)
        * "all"      — everything, no filter

    `q` is an optional search term. If `semantic=True`, pgvector cosine distance
    is used to retrieve semantically related papers even when keywords do not
    literally match.

    `kinds` restricts to specific `Paper.kind` values (e.g. `("video",
    "news")`).

    `quartiles` restricts to papers whose journal carries one of the given
    SJR quartiles (e.g. `("Q1",)`).
    """
    from sqlalchemy.orm import joinedload, selectinload

    query = (
        _user_papers_query(user, view)
        .join(Paper)
        .options(
            selectinload(UserPaper.notes),
            joinedload(UserPaper.paper).joinedload(Paper.video_summary),
            joinedload(UserPaper.paper).joinedload(Paper.journal),
        )
    )
    if kinds:
        query = query.filter(Paper.kind.in_(kinds))
    if quartiles:
        query = query.join(Journal, Paper.issn_l == Journal.issn_l).filter(
            Journal.sjr_quartile.in_(quartiles)
        )

    q = (q or "").strip()
    is_semantic_active = False
    if q and semantic:
        from app.modules.scrape.embedding_service import (
            current_embedding_model,
            get_embedding,
        )

        query_vector = get_embedding(q, user=user)
        if query_vector is not None:
            is_semantic_active = True
            like = _contains_pattern(q)
            # Only vectors this query vector can be compared with.
            comparable = db.and_(
                Paper.embedding.is_not(None),
                Paper.embedding_model == current_embedding_model(),
            )
            query = query.filter(
                db.or_(
                    db.and_(comparable, Paper.embedding.cosine_distance(query_vector) < 0.70),
                    db.func.lower(Paper.title).like(like, escape="\\"),
                    db.func.lower(Paper.abstract).like(like, escape="\\"),
                    db.func.lower(UserPaper.matched_keyword).like(like, escape="\\"),
                    db.func.lower(Paper.fulltext).like(like, escape="\\"),
                )
            ).order_by(
                db.case(
                    (comparable, Paper.embedding.cosine_distance(query_vector)),
                    else_=1.0,
                ).asc(),
                desc(Paper.published_at),
            )

    if q and not is_semantic_active:
        like = _contains_pattern(q)
        query = query.filter(
            db.or_(
                db.func.lower(Paper.title).like(like, escape="\\"),
                db.func.lower(Paper.abstract).like(like, escape="\\"),
                db.func.lower(UserPaper.matched_keyword).like(like, escape="\\"),
                db.func.lower(db.cast(Paper.authors, db.String)).like(like, escape="\\"),
                # Full text, for the papers whose licence let us keep it.
                # NULL for every other row, and `like` on NULL is NULL rather
                # than an error, so this simply never matches there -- no
                # extra guard needed, and no claim that the corpus is complete.
                db.func.lower(Paper.fulltext).like(like, escape="\\"),
            )
        )

    if not is_semantic_active:
        query = query.order_by(desc(Paper.published_at), desc(UserPaper.created_at))

    return query.limit(limit).all()


def list_user_papers_in_window(
    user: User, start: datetime, end: datetime, *, limit: int | None = None
) -> list[UserPaper]:
    """Papers newly surfaced for `user` inside [start, end) — the digest's
    cost/scope guard: only summarise what's actually new in the window,
    never the whole feed. Mirrors the "discover" view (dismissed excluded),
    ordered by publish date like `list_user_papers`.

    `limit` bounds the query itself rather than leaving it to the caller to
    slice an unbounded result — a busy user with dozens of active sources can
    otherwise land hundreds of rows (each pulling its notes via
    `selectinload`) just to have `ai_service.generate_digest` throw all but
    `DIGEST_MAX_ITEMS` away. Defaults to twice that constant rather than the
    exact figure: the caller still does its own scoring/truncation, so the
    query leaves it a little room to pick the best items rather than
    whatever the DB happened to return first. Resolved from `ai_service`
    lazily (not at module import) to avoid a circular import between the two
    service modules.
    """
    from sqlalchemy.orm import selectinload

    if limit is None:
        from app.modules.scrape.ai_service import DIGEST_MAX_ITEMS

        limit = DIGEST_MAX_ITEMS * 2

    query = (
        _user_papers_query(user, "discover")
        .join(Paper)
        .options(selectinload(UserPaper.notes))
        .filter(UserPaper.created_at >= start, UserPaper.created_at < end)
    )
    return query.order_by(desc(Paper.published_at), desc(UserPaper.created_at)).limit(limit).all()


def count_user_papers(user: User, view: str = "discover") -> int:
    """COUNT(*) for the same view filters list_user_papers uses. Library /
    Discover tab badges use this instead of materialising 500 rows just to
    take len() of them."""
    from sqlalchemy import func

    q = _user_papers_query(user, view).with_entities(func.count(UserPaper.id))
    return q.scalar() or 0


def search_user_papers_query(
    user: User,
    *,
    q: str | None = None,
    source: str | None = None,
    date_from=None,
    date_to=None,
    has_notes: bool = False,
    quartile: str | None = None,
    semantic: bool = False,
):
    """Return a SQLAlchemy query for the user's papers matching the filters.

    Returns a query (not a list) so the caller can `.paginate()`. Scoped to the
    user and hides dismissed papers — search is over the live library. All
    filters are ANDed; each is skipped when empty.

    If `semantic=True`, pgvector cosine distance ranks papers by semantic
    similarity to `q`.

    `quartile` ("Q1".."Q4") restricts to papers whose journal carries that SJR
    quartile; papers with no seeded journal are excluded, because "Q1 only" is
    a claim about the journal that an unknown one does not satisfy.

    Keyword search reads `Paper.fulltext` as well as the title and abstract,
    but that column is only populated for papers whose licence permitted
    keeping the text -- see `fulltext.REDISTRIBUTABLE_LICENSES`. So full-text
    matching covers a subset of the library and always will. That is a licence
    boundary, not a backlog: a paper that is free to read is not therefore free
    to store.
    """
    query = _live_library_query(user)

    q = (q or "").strip()
    is_semantic_active = False
    if q and semantic:
        query, is_semantic_active = _apply_semantic_search(query, q, user)
    if q and not is_semantic_active:
        query = query.filter(db.or_(*_text_match(_contains_pattern(q), include_authors=True)))
    if source:
        query = query.filter(Paper.source == source)
    if quartile:
        query = query.join(Journal, Paper.issn_l == Journal.issn_l).filter(
            Journal.sjr_quartile == quartile
        )
    if date_from is not None:
        query = query.filter(Paper.published_at >= date_from)
    if date_to is not None:
        query = query.filter(Paper.published_at <= date_to)
    if has_notes:
        # Only papers the user has written at least one note on.
        query = query.filter(UserPaper.notes.any())

    if not is_semantic_active:
        query = query.order_by(desc(Paper.published_at), desc(UserPaper.created_at))

    return query


def _live_library_query(user: User):
    """The user's non-dismissed papers, with the relations the paper card reads
    eager-loaded (same N+1 guard as list_user_papers — this feeds
    scrape/_paper_card.html via library/search.html too)."""
    from sqlalchemy.orm import joinedload, selectinload

    return (
        UserPaper.query.filter(UserPaper.user_id == user.id, UserPaper.dismissed_at.is_(None))
        .join(Paper)
        .options(
            selectinload(UserPaper.notes),
            joinedload(UserPaper.paper).joinedload(Paper.video_summary),
            joinedload(UserPaper.paper).joinedload(Paper.journal),
        )
    )


def _text_match(like: str, *, include_authors: bool) -> list:
    """Case-insensitive substring conditions over the searchable text columns.

    Full text is included: this function's caller has promised it since Faz 7.0
    and the query never did it -- which also meant a saved-search alert could
    not fire when OA full text arrived and put the term in the body, the case
    `e7b204c9f83a` says alerts exist for.
    """
    conditions = [
        db.func.lower(Paper.title).like(like, escape="\\"),
        db.func.lower(Paper.abstract).like(like, escape="\\"),
        db.func.lower(UserPaper.matched_keyword).like(like, escape="\\"),
    ]
    if include_authors:
        conditions.append(db.func.lower(db.cast(Paper.authors, db.String)).like(like, escape="\\"))
    conditions.append(db.func.lower(Paper.fulltext).like(like, escape="\\"))
    return conditions


def _apply_semantic_search(query, q: str, user: User):
    """Rank by pgvector cosine distance to `q` (keeping plain text matches as a
    fallback). Returns (query, active); inactive when no query vector is available."""
    from app.modules.scrape.embedding_service import current_embedding_model, get_embedding

    query_vector = get_embedding(q, user=user)
    if query_vector is None:
        return query, False
    # Only vectors this query vector can be compared with.
    comparable = db.and_(
        Paper.embedding.is_not(None),
        Paper.embedding_model == current_embedding_model(),
    )
    query = query.filter(
        db.or_(
            db.and_(comparable, Paper.embedding.cosine_distance(query_vector) < 0.70),
            *_text_match(_contains_pattern(q), include_authors=False),
        )
    ).order_by(
        db.case(
            (comparable, Paper.embedding.cosine_distance(query_vector)),
            else_=1.0,
        ).asc(),
        desc(Paper.published_at),
    )
    return query, True


def distinct_user_sources(user: User) -> list[str]:
    """The sources present in this user's library — powers the filter dropdown
    so it never offers a source the user has no papers from."""
    rows = (
        db.session.query(Paper.source)
        .join(UserPaper, UserPaper.paper_id == Paper.id)
        .filter(UserPaper.user_id == user.id, UserPaper.dismissed_at.is_(None))
        .distinct()
        .order_by(Paper.source)
        .all()
    )
    return [s for (s,) in rows]
