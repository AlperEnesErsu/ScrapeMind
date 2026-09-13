"""Patent tracking tables — a rolling window of USPTO full text.

Deliberately *not* rows in `papers`. An arXiv abstract is ~2 KB; a patent
description is 30-100 KB, and the write pattern is a weekly bulk load rather
than a nightly per-user scan. Mixing the two would drag `papers` and its HNSW
index into a workload neither was shaped for. See `docs/PHASE8.md` §2.

The window is the point: `grant_date` older than `PATENT_WINDOW_WEEKS` is
purged, so the corpus stays a fixed size instead of growing forever. A patent
a user actually kept lives on as a `papers` row — purging only drops the
corpus copy.
"""

from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from app.core.base_model import BaseModel
from app.extensions import db


class PatentDocument(BaseModel):
    __tablename__ = "patent_documents"

    # Optional bridge to the library. NULL is the normal state: being in the
    # corpus does not put a patent in anyone's library, and a purge must not
    # take the library row with it — hence SET NULL rather than CASCADE.
    paper_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("papers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # "US11234567B2" — country + number + kind, the form Google Patents and
    # every citation use. Unique because USPTO is the single authority here;
    # unlike `papers`, no second source competes to describe the same row.
    doc_number = db.Column(db.String(32), nullable=False, unique=True)
    kind_code = db.Column(db.String(4), nullable=True)  # B1 (no pre-grant pub), B2
    country = db.Column(db.String(2), nullable=False, default="US")

    title = db.Column(db.Text, nullable=False)
    abstract = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)

    # One weighted vector over title (A), abstract (B) and description (C).
    # Generated rather than trigger-maintained, so no ingest path can forget
    # it; `coalesce` because one NULL part would make the whole vector NULL.
    #
    # It replaced a description-only `description_tsv` (migration
    # a8d3e6f1b2c4): searching title and abstract meant building their vector
    # per row at query time, which measured ~117 ms on 3,000 documents before
    # ranking even started, and a zero-match query still paid it. The weights
    # also say something true -- a term in the title is a stronger signal than
    # the same term deep in a 50 KB description.
    search_tsv = db.Column(
        TSVECTOR,
        db.Computed(
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || setweight(to_tsvector('english', coalesce(abstract, '')), 'B') || setweight(to_tsvector('english', coalesce(description, '')), 'C')",
            persisted=True,
        ),
        nullable=True,
    )

    filing_date = db.Column(db.Date, nullable=True)
    # The column the window slides on, so it is indexed on its own *and*
    # paired with ai_source below.
    grant_date = db.Column(db.Date, nullable=True, index=True)
    priority_date = db.Column(db.Date, nullable=True)

    # JSON lists of strings, matching `Paper.authors`/`Paper.categories`.
    # Assignees are organisations and inventors are people; they are kept
    # apart here rather than prefix-tagged into one list the way
    # `patentsview_source` has to do to fit `PaperPayload`.
    # JSONB rather than `Paper`'s JSON: a GIN index — which is how "every
    # patent classified G06N3" stays cheap — is not available on `json`, only
    # on `jsonb`. The variant keeps SQLite working for anything that wants it.
    _json_list = db.JSON().with_variant(JSONB, "postgresql")
    assignees = db.Column(_json_list, nullable=True)
    inventors = db.Column(_json_list, nullable=True)
    cpc_codes = db.Column(_json_list, nullable=True)

    # Which rule let this document in: "cpc_core" (G06N) or "cpc_extended"
    # (G06V/G10L/G06F40). Stored so a count can say what definition produced
    # it — "how many AI patents" is entirely a function of where the line was
    # drawn. See `docs/PHASE8.md` §3.3.
    ai_source = db.Column(db.String(16), nullable=False, index=True)

    claim_count = db.Column(db.Integer, nullable=True)

    # Which weekly file this came from, and the hash of the document's own XML.
    # Together they make a re-parse decidable: same hash, nothing to rewrite.
    source_file = db.Column(db.String(128), nullable=True, index=True)
    raw_sha256 = db.Column(db.String(64), nullable=True)

    ingested_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())

    claims = db.relationship(
        "PatentClaim",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    chunks = db.relationship(
        "PatentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        db.Index("ix_patent_documents_ai_grant", "ai_source", "grant_date"),
        db.Index("ix_patent_documents_cpc_gin", "cpc_codes", postgresql_using="gin"),
        db.Index(
            "ix_patent_documents_search_tsv_gin",
            "search_tsv",
            postgresql_using="gin",
        ),
    )

    def __repr__(self) -> str:
        return f"<PatentDocument {self.doc_number}>"


class PatentClaim(BaseModel):
    __tablename__ = "patent_claims"

    patent_document_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("patent_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    number = db.Column(db.Integer, nullable=False)
    # An independent claim stands alone and defines scope; a dependent one
    # narrows another. `depends_on` is the parent's *number*, not a row id,
    # because that is what the XML gives and what renders the claim tree.
    is_independent = db.Column(db.Boolean, nullable=False, default=True)
    depends_on = db.Column(db.Integer, nullable=True)
    text = db.Column(db.Text, nullable=False)
    # Stored, not an expression index. A broad term ("network", "model")
    # appears in most claims, the planner rightly abandons the index and
    # scans -- and with only an expression index every scanned claim was
    # re-tokenised: ~1.4 s over 60k claims, paid once for the filter, again
    # for the ranking, again for the count. A stored vector turns the same
    # scan into a cheap tsvector comparison.
    text_tsv = db.Column(
        TSVECTOR,
        db.Computed("to_tsvector('english', text)", persisted=True),
        nullable=True,
    )

    document = db.relationship("PatentDocument", back_populates="claims")

    __table_args__ = (
        db.UniqueConstraint("patent_document_id", "number", name="uq_patent_claim_number"),
        db.Index("ix_patent_claims_text_tsv_gin", "text_tsv", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<PatentClaim {self.patent_document_id}#{self.number}>"


class PatentChunk(BaseModel):
    __tablename__ = "patent_chunks"

    patent_document_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("patent_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # v1 writes only "claim1". The column exists so adding "abstract" later is
    # a new row rather than a migration.
    kind = db.Column(db.String(16), nullable=False)
    ref = db.Column(db.String(32), nullable=True)  # e.g. the claim number
    text = db.Column(db.Text, nullable=False)

    # Same type as `Paper.embedding` on purpose — one embedding service, one
    # dimension, no per-table casting.
    embedding = db.Column(Vector(1536), nullable=True)
    # "model@dimension" that produced `embedding`. A query vector is only
    # comparable with stored vectors from the same model; change
    # EMBEDDING_MODEL and every old vector silently turns into noise that
    # still returns a confident-looking nearest neighbour. Search reads only
    # rows matching the current model and `embed_pending` redoes the rest.
    embedding_model = db.Column(db.String(96), nullable=True, index=True)

    document = db.relationship("PatentDocument", back_populates="chunks")

    __table_args__ = (
        db.UniqueConstraint("patent_document_id", "kind", "ref", name="uq_patent_chunk_ref"),
        db.Index(
            "ix_patent_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<PatentChunk {self.patent_document_id}/{self.kind}>"


class PatentIngestRun(BaseModel):
    """One weekly load. Deliberately not a `ScanRun`.

    `ScanRun` records a *user's* scan and drives the per-user status panel;
    this is system work with no user attached. Sharing the table would make
    "your last scan" answer a question the user never asked — the same
    reasoning that gave patents their own `ScanRun.kind` in Faz 5.2, applied
    one level further out.
    """

    __tablename__ = "patent_ingest_runs"

    source_file = db.Column(db.String(128), nullable=False, index=True)
    # "running" | "ok" | "partial" | "error" — same vocabulary as ScanRun.
    status = db.Column(db.String(16), nullable=False, default="running")
    documents_seen = db.Column(db.Integer, nullable=False, default=0)
    documents_kept = db.Column(db.Integer, nullable=False, default=0)
    started_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    error = db.Column(db.Text, nullable=True)

    def __repr__(self) -> str:
        return f"<PatentIngestRun {self.source_file} {self.status}>"
