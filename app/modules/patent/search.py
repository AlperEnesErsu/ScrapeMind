"""Full-text search over the patent window (Faz 8.4).

Real Postgres full-text search, not the `LIKE` the library search uses. The
difference matters here and not there: a library row is a title and an
abstract, while a patent carries a 30-100 KB description — a `LIKE '%x%'`
over that defeats the GIN indexes this module was built with and scans every
character of every description on every keystroke.

`websearch_to_tsquery` rather than `to_tsquery`: it accepts what people type
into a search box — quoted phrases, `or`, `-exclusion` — and never raises on
malformed input. `to_tsquery` turns a stray `&` or unbalanced parenthesis
into a 500.

**Claims are a separate scope, and that is the patent-specific part.** A
description *mentions* things; claims are what the patent *asserts*. "Does
anyone claim quantised attention?" and "does anyone mention it?" are
different questions with different answers, and only the first bears on
whether an idea is taken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from markupsafe import Markup, escape

from app.extensions import db
from app.modules.patent.models import PatentClaim, PatentDocument

PER_PAGE = 20

#: Highlight markers handed to `ts_headline`. Deliberately not HTML: the
#: headline is escaped first and only then are these swapped for <mark>, so
#: patent text can never inject markup. Nothing in a patent contains them.
_HL_START = "@@HLS@@"
_HL_END = "@@HLE@@"
_HEADLINE_OPTS = (
    f"StartSel={_HL_START}, StopSel={_HL_END}, MaxWords=35, MinWords=15, MaxFragments=2"
)

_MAX_QUERY = 300
_CPC_ALLOWED = re.compile(r"[^A-Z0-9/]")


def _escape_like(value: str) -> str:
    """User text inside LIKE must not act as a wildcard: an assignee search
    for `100%` would otherwise match every row."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


@dataclass
class SearchFilters:
    q: str = ""
    scope: str = "all"  # "all" | "claims"
    cpc: str = ""
    assignee: str = ""
    granted_from: date | None = None
    granted_to: date | None = None
    #: Fuse claim-1 vectors in with full text (8.5). Not a filter: it changes
    #: how results are found and ranked, never which structured rows qualify.
    semantic: bool = False

    @classmethod
    def from_args(cls, args) -> SearchFilters:
        """Parse request args, discarding rather than rejecting bad input.

        A malformed date or a CPC with stray characters narrows nothing; it
        does not turn a search page into an error page.
        """
        scope = (args.get("scope") or "all").strip()
        return cls(
            q=(args.get("q") or "").strip()[:_MAX_QUERY],
            scope=scope if scope in ("all", "claims") else "all",
            cpc=_CPC_ALLOWED.sub("", (args.get("cpc") or "").upper())[:16],
            assignee=(args.get("assignee") or "").strip()[:120],
            granted_from=_parse_date(args.get("from")),
            granted_to=_parse_date(args.get("to")),
            semantic=args.get("semantic") == "1",
        )

    @property
    def is_empty(self) -> bool:
        return not (self.q or self.cpc or self.assignee or self.granted_from or self.granted_to)


def query_is_meaningful(q: str) -> bool:
    """False when the text reduces to nothing searchable.

    `websearch_to_tsquery('english', 'the of and')` is an empty query: every
    word is a stopword. Matched with `@@` it silently finds nothing, which
    reads as "no patent covers this" — the one wrong conclusion this page
    must not invite. The caller says so instead.
    """
    if not q:
        return False
    return bool(
        db.session.execute(
            db.select(db.func.numnode(db.func.websearch_to_tsquery("english", q)) > 0)
        ).scalar()
    )


def _tsquery(q: str):
    return db.func.websearch_to_tsquery("english", q)


def _claim_matched_ids(tsq):
    """Documents with at least one matching claim, computed once.

    Two measured problems shaped this. A correlated `EXISTS` per document ran
    one subquery per row. The uncorrelated set that replaced it was then
    planned *twice* -- once inside the ranking `CASE`, once in the filter --
    and each was a full scan for a broad term. A CTE referenced twice is
    materialised by Postgres, so the claim match now happens exactly once,
    against the stored `text_tsv`.
    """
    return (
        db.select(PatentClaim.patent_document_id.label("doc_id"))
        .where(PatentClaim.text_tsv.op("@@")(tsq))
        .distinct()
        .cte("claim_hits")
    )


def build_query(filters: SearchFilters):
    """A query over live documents, ANDing every filter that is set.

    Ordering with a text query: a match in the claims outranks any amount of
    description matching, then `ts_rank` decides. Without one: newest grant
    first, which is what the tracker is for.
    """
    query = PatentDocument.query.filter(PatentDocument.deleted_at.is_(None))

    if filters.cpc:
        query = query.filter(
            db.text(
                "EXISTS (SELECT 1 FROM jsonb_array_elements_text(patent_documents.cpc_codes) c "
                "WHERE c LIKE :cpc_prefix ESCAPE '\\')"
            ).bindparams(cpc_prefix=_escape_like(filters.cpc) + "%")
        )
    if filters.assignee:
        query = query.filter(
            db.text(
                "EXISTS (SELECT 1 FROM jsonb_array_elements_text(patent_documents.assignees) a "
                "WHERE a ILIKE :assignee_like ESCAPE '\\')"
            ).bindparams(assignee_like="%" + _escape_like(filters.assignee) + "%")
        )
    if filters.granted_from:
        query = query.filter(PatentDocument.grant_date >= filters.granted_from)
    if filters.granted_to:
        query = query.filter(PatentDocument.grant_date <= filters.granted_to)

    if filters.q and query_is_meaningful(filters.q):
        tsq = _tsquery(filters.q)
        hits = _claim_matched_ids(tsq)
        in_claims = PatentDocument.id.in_(db.select(hits.c.doc_id))
        if filters.scope == "claims":
            query = query.filter(in_claims)
        else:
            query = query.filter(db.or_(in_claims, PatentDocument.search_tsv.op("@@")(tsq)))
        # A claim match adds a full point, and normalisation flag 32
        # (rank / (rank + 1)) bounds `ts_rank` to [0, 1). Without the flag the
        # rank is unbounded, and a description repeating a term often enough
        # could overtake a patent that actually claims it -- the ordering
        # guarantee has to hold by construction, not by the data being tame.
        rank = db.case((in_claims, 1.0), else_=0.0) + db.func.ts_rank(
            PatentDocument.search_tsv, tsq, 32
        )
        return query.order_by(
            rank.desc(), PatentDocument.grant_date.desc(), PatentDocument.id.desc()
        )

    return query.order_by(PatentDocument.grant_date.desc(), PatentDocument.id.desc())


def _highlight(raw: str) -> Markup:
    """Escape first, then turn the markers into <mark>. Order is the safety.

    Takes a non-empty headline only; callers skip empty ones, so a `Snippet`
    can never be built around nothing.
    """
    safe = str(escape(raw))
    return Markup(safe.replace(_HL_START, "<mark>").replace(_HL_END, "</mark>"))


@dataclass
class Snippet:
    """Why a result matched. `claim_number` is None for a description hit."""

    text: Markup
    claim_number: int | None


def snippets_for(
    documents: list[PatentDocument], q: str, *, scope: str = "all"
) -> dict[int, Snippet]:
    """One highlighted excerpt per document on the current page.

    Computed for the page only — `ts_headline` re-parses the whole text and is
    far too expensive to run across every match. A claim hit is preferred over
    a description hit, and the lowest-numbered matching claim wins, because
    claim 1 is the scope and the reader should see it if it is the match.
    """
    if not documents or not q or not query_is_meaningful(q):
        return {}
    ids = [d.id for d in documents]
    tsq = _tsquery(q)
    out: dict[int, Snippet] = {}

    claim_rows = db.session.execute(
        db.select(
            PatentClaim.patent_document_id,
            PatentClaim.number,
            db.func.ts_headline("english", PatentClaim.text, tsq, _HEADLINE_OPTS),
        )
        .where(
            PatentClaim.patent_document_id.in_(ids),
            PatentClaim.text_tsv.op("@@")(tsq),
        )
        .order_by(PatentClaim.patent_document_id, PatentClaim.number)
    ).all()
    for doc_id, number, headline in claim_rows:
        if headline and doc_id not in out:
            out[doc_id] = Snippet(text=_highlight(headline), claim_number=number)

    if scope == "claims":
        return out

    missing = [i for i in ids if i not in out]
    if missing:
        desc_rows = db.session.execute(
            db.select(
                PatentDocument.id,
                db.func.ts_headline(
                    "english",
                    db.func.coalesce(PatentDocument.abstract, "")
                    + " "
                    + db.func.coalesce(PatentDocument.description, ""),
                    tsq,
                    _HEADLINE_OPTS,
                ),
            ).where(PatentDocument.id.in_(missing))
        ).all()
        for doc_id, headline in desc_rows:
            if headline and _HL_START in headline:
                out[doc_id] = Snippet(text=_highlight(headline), claim_number=None)

    return out


# --- Semantic + hybrid (Faz 8.5) ------------------------------------------

#: Reciprocal Rank Fusion constant from Cormack et al. (2009), the value every
#: RRF implementation uses. Large enough that rank 1 does not dominate.
RRF_K = 60
#: How deep each list goes before fusing. Pages are fused in Python, so this
#: bounds the work; a hybrid query almost never pages past its first 200.
CANDIDATES = 100
#: Cosine distance beyond which a neighbour is not treated as a match. Vector
#: search always returns *something* -- without a floor, a query with no real
#: match would still fill the page with the least-distant noise. 0.70 is the
#: value the library's semantic search already uses.
MAX_DISTANCE_DEFAULT = 0.70


def fuse(*rankings: list[int], k: int = RRF_K) -> list[int]:
    """Reciprocal Rank Fusion over ranked id lists, best first.

    `score(d) = sum(1 / (k + rank))` over every list containing d. Chosen over
    blending the raw scores because `ts_rank` and cosine distance are on
    unrelated scales -- any weighted sum needs calibration that the data
    would quietly break. RRF only uses positions.

    Ties break on first appearance, so the order is deterministic and the
    earlier list (full text, where a claim match is guaranteed first) wins.
    """
    scores: dict[int, float] = {}
    first_seen: dict[int, int] = {}
    position = 0
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            if doc_id not in first_seen:
                first_seen[doc_id] = position
                position += 1
    return sorted(scores, key=lambda d: (-scores[d], first_seen[d]))


class ListPagination:
    """The slice of `flask_sqlalchemy.Pagination` that `_pagination.html` uses,
    over a list already in memory -- a fused ranking is not a SQL query."""

    def __init__(self, items: list, page: int, per_page: int, total: int):
        self.items = items
        self.page = page
        self.per_page = per_page
        self.total = total

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page)) if self.total else 0

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def prev_num(self) -> int | None:
        return self.page - 1 if self.has_prev else None

    @property
    def next_num(self) -> int | None:
        return self.page + 1 if self.has_next else None

    def iter_pages(self, left_edge=2, left_current=2, right_current=4, right_edge=2):
        """Same contract as Flask-SQLAlchemy: page numbers, None for a gap."""
        last = 0
        for num in range(1, self.pages + 1):
            if (
                num <= left_edge
                or self.page - left_current <= num <= self.page + right_current
                or num > self.pages - right_edge
            ):
                if last + 1 != num:
                    yield None
                yield num
                last = num


@dataclass
class HybridResult:
    pagination: ListPagination
    semantic_used: bool
    #: doc id -> cosine similarity (1 - distance), for results the vector list found.
    similarity: dict[int, float]
    #: Full-text matches before the candidate cut. Fusion only ever sees the
    #: top `CANDIDATES` of each list, so the fused total is a ceiling, not a
    #: count -- showing it alone would tell someone searching a common term
    #: that 100 patents match when 3,000 do.
    text_total: int = 0

    @property
    def capped(self) -> bool:
        return self.text_total > CANDIDATES


def _filtered_ids_query(filters: SearchFilters):
    """The structured filters alone, as an id subquery both candidate lists share."""
    no_text = SearchFilters(
        cpc=filters.cpc,
        assignee=filters.assignee,
        granted_from=filters.granted_from,
        granted_to=filters.granted_to,
    )
    return build_query(no_text).with_entities(PatentDocument.id).order_by(None)


def semantic_candidates(
    query_vector: list[float], filters: SearchFilters, *, max_distance: float
) -> list[tuple[int, float]]:
    """Nearest claim-1 vectors from the current model, filtered, best first."""
    from app.modules.patent.embedding import KIND_CLAIM1, current_model
    from app.modules.patent.models import PatentChunk

    distance = PatentChunk.embedding.cosine_distance(query_vector)
    rows = db.session.execute(
        db.select(PatentChunk.patent_document_id, distance)
        .where(
            PatentChunk.kind == KIND_CLAIM1,
            PatentChunk.embedding.is_not(None),
            # Only vectors this query vector can be compared with.
            PatentChunk.embedding_model == current_model(),
            PatentChunk.patent_document_id.in_(_filtered_ids_query(filters)),
            distance <= max_distance,
        )
        .order_by(distance)
        .limit(CANDIDATES)
    ).all()
    return [(doc_id, float(dist)) for doc_id, dist in rows]


def hybrid_search(filters: SearchFilters, page: int, *, user=None) -> HybridResult:
    """Full text and claim-1 vectors, fused with RRF.

    Falls back to full text alone -- and says so through `semantic_used` --
    when no vector can be made for the query: no provider, a failed call, or
    a deployment set to full text only. Silence there would present lexical
    results as semantic ones.
    """
    from app.modules.patent.embedding import is_enabled
    from app.modules.scrape.embedding_service import get_embedding

    text_query = build_query(filters)
    text_ids = [d.id for d in text_query.limit(CANDIDATES).all()]
    text_total = text_query.order_by(None).count()

    vector_hits: list[tuple[int, float]] = []
    semantic_used = False
    if filters.q and is_enabled():
        query_vector = get_embedding(filters.q, user=user)
        if query_vector is not None:
            from flask import current_app

            max_distance = float(
                current_app.config.get("PATENT_SEMANTIC_MAX_DISTANCE", MAX_DISTANCE_DEFAULT)
            )
            vector_hits = semantic_candidates(query_vector, filters, max_distance=max_distance)
            semantic_used = True

    fused = fuse(text_ids, [doc_id for doc_id, _ in vector_hits])
    page = max(page, 1)
    window = fused[(page - 1) * PER_PAGE : page * PER_PAGE]
    by_id = (
        {d.id: d for d in PatentDocument.query.filter(PatentDocument.id.in_(window)).all()}
        if window
        else {}
    )
    items = [by_id[i] for i in window if i in by_id]
    return HybridResult(
        pagination=ListPagination(items, page, PER_PAGE, len(fused)),
        semantic_used=semantic_used,
        similarity={doc_id: round(1.0 - dist, 3) for doc_id, dist in vector_hits},
        text_total=text_total,
    )


def scope_claim_snippets(documents: list[PatentDocument], exclude: set[int]) -> dict[int, Snippet]:
    """For results found only by meaning: show the claim that was matched.

    A semantic hit has no highlighted words -- the query may share none with
    the patent -- so the honest excerpt is the claim-1 text the vector was
    made from, labelled as such rather than dressed up with fake highlights.
    """
    from app.modules.patent.embedding import KIND_CLAIM1
    from app.modules.patent.models import PatentChunk

    ids = [d.id for d in documents if d.id not in exclude]
    if not ids:
        return {}
    rows = db.session.execute(
        db.select(PatentChunk.patent_document_id, PatentChunk.ref, PatentChunk.text).where(
            PatentChunk.patent_document_id.in_(ids), PatentChunk.kind == KIND_CLAIM1
        )
    ).all()
    out: dict[int, Snippet] = {}
    for doc_id, ref, text in rows:
        excerpt = text if len(text) <= 320 else text[:320].rsplit(" ", 1)[0] + " …"
        out[doc_id] = Snippet(
            text=Markup(escape(excerpt)),
            claim_number=int(ref) if ref and ref.isdigit() else None,
        )
    return out
