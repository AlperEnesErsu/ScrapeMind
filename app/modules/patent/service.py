"""Reads over the patent window.

Queries and shaping only — the weekly load that fills these tables arrives in
Phase 8.3. Everything here has to behave on an empty corpus, because until
that lands an empty corpus is the *normal* state, not an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from flask import current_app

from app.extensions import db
from app.modules.patent.models import PatentClaim, PatentDocument, PatentIngestRun

DEFAULT_WINDOW_WEEKS = 3


def window_weeks() -> int:
    """How many weeks the corpus keeps. Read per call, not at import, so a
    test or an admin change takes effect without a restart."""
    try:
        weeks = int(current_app.config.get("PATENT_WINDOW_WEEKS", DEFAULT_WINDOW_WEEKS))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_WEEKS
    return max(weeks, 1)


def window_start(now: date | None = None) -> date:
    """The oldest `grant_date` the window keeps. The purge in 8.3 and the
    "showing N weeks" line in the UI must agree, so both call this."""
    today = now or datetime.now(UTC).date()
    return today - timedelta(weeks=window_weeks())


@dataclass
class WindowStats:
    documents: int = 0
    claims: int = 0
    oldest: date | None = None
    newest: date | None = None
    weeks: int = 0
    last_run: PatentIngestRun | None = None

    @property
    def is_empty(self) -> bool:
        return self.documents == 0


def window_stats() -> WindowStats:
    """One row of numbers for the index page and the admin panel."""
    documents, oldest, newest = (
        db.session.query(
            db.func.count(PatentDocument.id),
            db.func.min(PatentDocument.grant_date),
            db.func.max(PatentDocument.grant_date),
        )
        .filter(PatentDocument.deleted_at.is_(None))
        .one()
    )
    claims = (
        db.session.query(db.func.count(PatentClaim.id))
        .join(PatentDocument, PatentClaim.patent_document_id == PatentDocument.id)
        .filter(PatentDocument.deleted_at.is_(None))
        .scalar()
    )
    last_run = (
        PatentIngestRun.query.filter(PatentIngestRun.deleted_at.is_(None))
        .order_by(PatentIngestRun.started_at.desc())
        .first()
    )
    return WindowStats(
        documents=documents or 0,
        claims=claims or 0,
        oldest=oldest,
        newest=newest,
        weeks=window_weeks(),
        last_run=last_run,
    )


def recent_documents(limit: int = 50) -> list[PatentDocument]:
    """Newest grants first — the question the tracker exists to answer.

    `doc_number` breaks ties: a whole week of grants shares one `grant_date`,
    and without a second key the "latest" list would reshuffle between two
    identical requests.
    """
    return (
        PatentDocument.query.filter(PatentDocument.deleted_at.is_(None))
        .order_by(PatentDocument.grant_date.desc(), PatentDocument.doc_number.desc())
        .limit(limit)
        .all()
    )


def get_document(doc_number: str) -> PatentDocument | None:
    return PatentDocument.query.filter(
        PatentDocument.doc_number == doc_number,
        PatentDocument.deleted_at.is_(None),
    ).first()


@dataclass
class ClaimNode:
    claim: PatentClaim
    children: list[ClaimNode] = field(default_factory=list)


def claim_tree(document: PatentDocument) -> list[ClaimNode]:
    """Independent claims at the root, dependants nested under their parent.

    This is the shape a patent is actually read in: claim 1 sets the scope and
    every dependent claim narrows something above it. A flat numbered list —
    which is what every patent site shows — hides that structure completely.

    Three defensive cases, all of which occur in real grants:

    - `depends_on` naming a claim that is not in this document (a cross-
      reference, or a parse miss) — the claim becomes a root rather than
      vanishing from the page.
    - A cycle, which no valid patent has but a bad parse can produce. Nodes
      already attached are never re-attached, so a cycle degrades to roots
      instead of recursing forever.
    - Claims arriving out of order; the parent may not be seen yet, so the
      pass over `depends_on` happens after every node exists.
    """
    nodes = {c.number: ClaimNode(claim=c) for c in sorted(document.claims, key=lambda c: c.number)}
    roots: list[ClaimNode] = []

    for number, node in nodes.items():
        parent_number = node.claim.depends_on
        parent = nodes.get(parent_number) if parent_number is not None else None
        if parent is None or parent_number == number or _is_descendant(node, parent, nodes):
            roots.append(node)
            continue
        parent.children.append(node)

    return roots


def _is_descendant(
    node: ClaimNode, candidate_parent: ClaimNode, nodes: dict[int, ClaimNode]
) -> bool:
    """Would attaching `node` under `candidate_parent` close a loop?"""
    seen: set[int] = set()
    cursor: ClaimNode | None = candidate_parent
    while cursor is not None:
        number = cursor.claim.number
        if number == node.claim.number:
            return True
        if number in seen:
            return True
        seen.add(number)
        parent_number = cursor.claim.depends_on
        cursor = nodes.get(parent_number) if parent_number is not None else None
    return False


def recent_runs(limit: int = 10) -> list[PatentIngestRun]:
    """Newest first, for the admin panel. `id` breaks ties because two manual
    triggers in the same second share a `started_at`."""
    return (
        PatentIngestRun.query.filter(PatentIngestRun.deleted_at.is_(None))
        .order_by(PatentIngestRun.started_at.desc(), PatentIngestRun.id.desc())
        .limit(limit)
        .all()
    )


@dataclass
class LoadSettings:
    """What the next load will do, stated before anyone presses the button.

    `has_key` decides whether there will be a load at all: without an ODP API
    key the weekly run is skipped, and the panel has to say so plainly --
    otherwise an admin sees a button and a quiet week and cannot tell why.
    """

    window_weeks: int
    cpc_core: str
    cpc_extended: str
    has_key: bool
    #: Semantic search readiness (8.5). `embedded` of `total` documents have a
    #: claim-1 vector from `embedding_model`; the rest are invisible to
    #: meaning-based matching until `embed_pending` reaches them.
    embeddings_enabled: bool = False
    embedded: int = 0
    total: int = 0
    embedding_model: str = ""


def load_settings() -> LoadSettings:
    from app.modules.patent import embedding, uspto

    has_key = uspto.credentials_ok()
    embedded, total = embedding.coverage()
    return LoadSettings(
        window_weeks=window_weeks(),
        cpc_core=current_app.config.get("PATENT_AI_CPC_CODES") or "G06N",
        cpc_extended=current_app.config.get("PATENT_AI_CPC_EXTENDED") or "",
        has_key=has_key,
        # With a key the file is discovered at run time, so there is nothing
        # honest to show in advance. Without one it is a pure function of the
        # date and can be shown exactly.
        embeddings_enabled=embedding.is_enabled(),
        embedded=embedded,
        total=total,
        embedding_model=embedding.current_model(),
    )
