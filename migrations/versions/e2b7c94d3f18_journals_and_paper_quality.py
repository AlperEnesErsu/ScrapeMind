"""journals table + papers.issn_l / papers.cited_by_count

Revision ID: e2b7c94d3f18
Revises: d8a1c6e40f27
Create Date: 2026-08-12 10:00:00.000000

`papers.issn_l` is a plain indexed column, not a foreign key to `journals`: a
paper's ISSN is a fact regardless of whether the Scimago seed knows that
journal, and an FK would force either a rejected insert or a placeholder row
for every unseeded ISSN. The join is done on the column.

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e2b7c94d3f18"
down_revision = "d8a1c6e40f27"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "journals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("issn_l", sa.String(length=9), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("sjr", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("sjr_quartile", sa.String(length=2), nullable=True),
        sa.Column("sjr_year", sa.Integer(), nullable=True),
        sa.Column("h_index", sa.Integer(), nullable=True),
        sa.Column("is_doaj", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_oa", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_journals_issn_l"), "journals", ["issn_l"], unique=True)
    # Indexed because the library filter selects on it (?quartile=Q1) — a
    # quartile filter over a full papers→journals join is the one query here
    # that would otherwise scan.
    op.create_index(op.f("ix_journals_sjr_quartile"), "journals", ["sjr_quartile"], unique=False)
    op.create_index(op.f("ix_journals_deleted_at"), "journals", ["deleted_at"], unique=False)

    op.add_column("papers", sa.Column("issn_l", sa.String(length=9), nullable=True))
    op.add_column("papers", sa.Column("cited_by_count", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_papers_issn_l"), "papers", ["issn_l"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_papers_issn_l"), table_name="papers")
    op.drop_column("papers", "cited_by_count")
    op.drop_column("papers", "issn_l")

    op.drop_index(op.f("ix_journals_deleted_at"), table_name="journals")
    op.drop_index(op.f("ix_journals_sjr_quartile"), table_name="journals")
    op.drop_index(op.f("ix_journals_issn_l"), table_name="journals")
    op.drop_table("journals")
