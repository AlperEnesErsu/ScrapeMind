"""patent search: stored vectors for documents and claims

Revision ID: a8d3e6f1b2c4
Revises: b1e4c7a90d2f
Create Date: 2026-09-13 11:40:00.000000

Replaces `description_tsv` rather than adding beside it, and the reason is a
measurement, not a preference.

Search has to match title and abstract as well as the description. With only
a description vector, the other two were turned into a vector per row at
query time: ~117 ms on 3,000 documents averaging 56 KB, spent before ranking
started, and paid in full by a query that matched nothing because the OR
with an unindexed expression forces a scan. A generated, weighted column
moves that cost to write time and puts all three behind one GIN index.

`description_tsv` is dropped rather than kept for compatibility: nothing reads
it outside this module, and keeping both would store the largest part of every
vector -- the description -- twice.

Weights: title A, abstract B, description C, so `ts_rank` prefers a term in
the title to the same term buried in the body.

Claims get the same treatment, for a second measured reason. Their only
full-text support was an expression index on `to_tsvector('english', text)`.
A broad term appears in most claims (49k of 60k in the benchmark), so the
planner correctly skips the index and scans -- and an expression index gives
a scan nothing, so every claim was re-tokenised: ~1.4 s, once for the filter,
once for the ranking, once for the count. A stored `text_tsv` makes the same
scan a comparison. Together these took a broad-term search from ~4.3 s to
the figure recorded in docs/PHASE8.md section 9.4.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a8d3e6f1b2c4"
# Parented on the merge revision, not on `c3f9a17d40be` where it was written.
# Re-parenting is safe here and only here: this revision had never been applied
# to a persistent database when it moved (scratch databases only), so no stamp
# can disagree with it. Contrast `b1e4c7a90d2f`, which exists precisely because
# `c3f9a17d40be` *had* been applied and could not be moved.
down_revision = "b1e4c7a90d2f"
branch_labels = None
depends_on = None

_SEARCH_EXPR = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(abstract, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(description, '')), 'C')"
)


def upgrade():
    op.execute("DROP INDEX IF EXISTS ix_patent_documents_tsv_gin")
    op.execute("ALTER TABLE patent_documents DROP COLUMN IF EXISTS description_tsv")
    op.execute(
        "ALTER TABLE patent_documents ADD COLUMN search_tsv tsvector "
        f"GENERATED ALWAYS AS ({_SEARCH_EXPR}) STORED"
    )
    op.execute(
        "CREATE INDEX ix_patent_documents_search_tsv_gin "
        "ON patent_documents USING gin (search_tsv)"
    )

    op.execute("DROP INDEX IF EXISTS ix_patent_claims_text_gin")
    op.execute(
        "ALTER TABLE patent_claims ADD COLUMN text_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED"
    )
    op.execute("CREATE INDEX ix_patent_claims_text_tsv_gin ON patent_claims USING gin (text_tsv)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_patent_claims_text_tsv_gin")
    op.execute("ALTER TABLE patent_claims DROP COLUMN IF EXISTS text_tsv")
    op.execute(
        "CREATE INDEX ix_patent_claims_text_gin ON patent_claims "
        "USING gin (to_tsvector('english', text))"
    )

    op.execute("DROP INDEX IF EXISTS ix_patent_documents_search_tsv_gin")
    op.execute("ALTER TABLE patent_documents DROP COLUMN IF EXISTS search_tsv")
    op.execute(
        "ALTER TABLE patent_documents ADD COLUMN description_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', coalesce(description, ''))) STORED"
    )
    op.execute(
        "CREATE INDEX ix_patent_documents_tsv_gin ON patent_documents USING gin (description_tsv)"
    )
