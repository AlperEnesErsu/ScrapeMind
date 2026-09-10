"""papers: open-access metadata and the full-text columns

Revision ID: d51a7c2e04b9
Revises: c4e91b0a77d2
Create Date: 2026-09-10 19:05:00.000000

`papers.pdf_url` already existed but described nothing about access: OpenAlex
filled it from `primary_location` whenever `best_oa_location` was missing, and
a primary location is the publisher's -- routinely paywalled. So a non-empty
`pdf_url` was never evidence that anything could be fetched.

`oa_status` / `oa_license` / `oa_url` state access explicitly. `oa_url` is only
ever written from `best_oa_location`, which is what makes it safe to fetch.

`fulltext_chars` is recorded even when `fulltext` is not, the same way
`video_summaries.transcript_chars` is: it separates "fetched, 41k characters"
from "never fetched", which a NULL body cannot express on its own.

`fulltext` is populated only for licences that permit redistribution -- see
`REDISTRIBUTABLE_LICENSES` in app/modules/scrape/fulltext.py. Open access
governs reading, not republishing, and bronze OA in particular is free to read
with no licence at all.

Nullable throughout, no backfill: every existing row means "never looked",
which is true.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d51a7c2e04b9"
down_revision = "c4e91b0a77d2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("papers") as batch:
        batch.add_column(sa.Column("oa_status", sa.String(16), nullable=True))
        batch.add_column(sa.Column("oa_license", sa.String(64), nullable=True))
        batch.add_column(sa.Column("oa_url", sa.Text(), nullable=True))
        batch.add_column(sa.Column("fulltext_chars", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("fulltext_fetched_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("fulltext", sa.Text(), nullable=True))

    # Indexed because the fetch task's queue is "OA papers with no full text
    # yet", which filters on this column across the whole table.
    op.create_index("ix_papers_oa_status", "papers", ["oa_status"])


def downgrade():
    op.drop_index("ix_papers_oa_status", table_name="papers")
    with op.batch_alter_table("papers") as batch:
        batch.drop_column("fulltext")
        batch.drop_column("fulltext_fetched_at")
        batch.drop_column("fulltext_chars")
        batch.drop_column("oa_url")
        batch.drop_column("oa_license")
        batch.drop_column("oa_status")
