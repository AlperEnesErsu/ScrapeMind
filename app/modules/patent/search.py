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


def _highlight(raw: str | None) -> Markup | None:
    """Escape first, then turn the markers into <mark>. Order is the safety."""
    if not raw:
        return None
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
        if doc_id not in out:
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
