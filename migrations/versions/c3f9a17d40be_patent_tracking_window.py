"""patent tracking — a rolling window of USPTO full text

Revision ID: c3f9a17d40be
Revises: e7b204c9f83a
Create Date: 2026-09-12 13:05:00.000000

Four tables rather than columns on `papers`, and that is the design decision.

A patent description is 30-100 KB against an arXiv abstract's ~2 KB, and it
arrives as a weekly bulk load rather than a nightly per-user scan. Putting it
in `papers` would drag that table -- and the HNSW index every semantic search
already uses -- into a workload it was not shaped for. `paper_id` is the
bridge, and it is nullable on purpose: a corpus document does not imply a
library row, and the rolling purge must never take a library row with it
(SET NULL, not CASCADE).

`cpc_codes` is JSONB where `papers.categories` is JSON, deliberately. GIN --
which is what makes "every patent classified G06N3" cheap -- does not exist
for `json`, only for `jsonb`. The cost is that the two tables disagree about a
type; the alternative was a sequential scan over the whole window.

`description_tsv` is a generated column, not a trigger: Postgres keeps it
current on its own, so no ingest path can forget it. The HNSW index is created
here rather than deferred until after the first load because the window holds
~5k vectors, where the build is instant -- the deferral a 500k-vector corpus
would have needed is exactly the complexity this window buys out of.
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c3f9a17d40be"
down_revision = "e7b204c9f83a"
branch_labels = None
depends_on = None


def _timestamps():
    """The BaseModel columns every table in this project carries."""
    return [
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade():
    op.create_table(
        "patent_documents",
        *_timestamps(),
        sa.Column("paper_id", sa.BigInteger(), nullable=True),
        sa.Column("doc_number", sa.String(length=32), nullable=False),
        sa.Column("kind_code", sa.String(length=4), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "description_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(description, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column("filing_date", sa.Date(), nullable=True),
        sa.Column("grant_date", sa.Date(), nullable=True),
        sa.Column("priority_date", sa.Date(), nullable=True),
        sa.Column("assignees", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("inventors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cpc_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ai_source", sa.String(length=16), nullable=False),
        sa.Column("claim_count", sa.Integer(), nullable=True),
        sa.Column("source_file", sa.String(length=128), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("doc_number"),
    )
    op.create_index("ix_patent_documents_deleted_at", "patent_documents", ["deleted_at"])
    op.create_index("ix_patent_documents_paper_id", "patent_documents", ["paper_id"])
    op.create_index("ix_patent_documents_grant_date", "patent_documents", ["grant_date"])
    op.create_index("ix_patent_documents_ai_source", "patent_documents", ["ai_source"])
    op.create_index("ix_patent_documents_source_file", "patent_documents", ["source_file"])
    op.create_index(
        "ix_patent_documents_ai_grant",
        "patent_documents",
        ["ai_source", "grant_date"],
    )
    op.create_index(
        "ix_patent_documents_cpc_gin",
        "patent_documents",
        ["cpc_codes"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_patent_documents_tsv_gin",
        "patent_documents",
        ["description_tsv"],
        postgresql_using="gin",
    )

    op.create_table(
        "patent_claims",
        *_timestamps(),
        sa.Column("patent_document_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("is_independent", sa.Boolean(), nullable=False),
        sa.Column("depends_on", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["patent_document_id"], ["patent_documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("patent_document_id", "number", name="uq_patent_claim_number"),
    )
    op.create_index("ix_patent_claims_deleted_at", "patent_claims", ["deleted_at"])
    op.create_index(
        "ix_patent_claims_patent_document_id", "patent_claims", ["patent_document_id"]
    )
    # Expression index -- op.create_index cannot express a function call.
    op.execute(
        "CREATE INDEX ix_patent_claims_text_gin ON patent_claims "
        "USING gin (to_tsvector('english', text))"
    )

    op.create_table(
        "patent_chunks",
        *_timestamps(),
        sa.Column("patent_document_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ref", sa.String(length=32), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.ForeignKeyConstraint(
            ["patent_document_id"], ["patent_documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("patent_document_id", "kind", "ref", name="uq_patent_chunk_ref"),
    )
    op.create_index("ix_patent_chunks_deleted_at", "patent_chunks", ["deleted_at"])
    op.create_index(
        "ix_patent_chunks_patent_document_id", "patent_chunks", ["patent_document_id"]
    )
    op.create_index(
        "ix_patent_chunks_embedding_hnsw",
        "patent_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "patent_ingest_runs",
        *_timestamps(),
        sa.Column("source_file", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("documents_seen", sa.Integer(), nullable=False),
        sa.Column("documents_kept", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_patent_ingest_runs_deleted_at", "patent_ingest_runs", ["deleted_at"])
    op.create_index("ix_patent_ingest_runs_source_file", "patent_ingest_runs", ["source_file"])


def downgrade():
    op.drop_table("patent_ingest_runs")
    op.drop_table("patent_chunks")
    # Dropped explicitly: the table drop would take it, but naming it keeps the
    # downgrade readable next to the raw CREATE INDEX above.
    op.execute("DROP INDEX IF EXISTS ix_patent_claims_text_gin")
    op.drop_table("patent_claims")
    op.drop_table("patent_documents")
