"""saved searches, and the record of what has already been announced

Revision ID: e7b204c9f83a
Revises: d51a7c2e04b9
Create Date: 2026-09-10 20:15:00.000000

`saved_search_notifications` looks like bookkeeping and is the design decision.

The cheap implementation of "alert me about new matches" is a watermark on the
search -- announce whatever matches and was created since the last run. The
digest can work that way because its question is "what arrived in this window".
A saved search's question is different: a paper can start matching long after
it arrived, when a DOI match fills an empty abstract, or the OA full text lands
and puts the term in the body. Those papers are not new, and they are exactly
the moment the feature exists for. A watermark drops every one of them silently.

So the row is per (search, paper) announced, and the unique constraint over the
pair is the real guarantee -- two overlapping runs cannot both announce the
same paper regardless of what the application does between them.

`saved_searches.filters` is JSON rather than columns because it mirrors the
keyword arguments of `search_user_papers_query`, a signature that has already
grown twice. A new filter should need one migration, not two.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e7b204c9f83a"
down_revision = "d51a7c2e04b9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("q", sa.Text(), nullable=True),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("semantic", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("cadence", sa.String(16), nullable=False, server_default="weekly"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_saved_search_user_name"),
    )
    op.create_index("ix_saved_searches_user_id", "saved_searches", ["user_id"])

    op.create_table(
        "saved_search_notifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("saved_search_id", sa.BigInteger(), nullable=False),
        sa.Column("paper_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["saved_search_id"], ["saved_searches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "saved_search_id", "paper_id", name="uq_saved_search_notification"
        ),
    )
    op.create_index(
        "ix_saved_search_notifications_search",
        "saved_search_notifications",
        ["saved_search_id"],
    )
    op.create_index(
        "ix_saved_search_notifications_paper", "saved_search_notifications", ["paper_id"]
    )


def downgrade():
    op.drop_index("ix_saved_search_notifications_paper", table_name="saved_search_notifications")
    op.drop_index("ix_saved_search_notifications_search", table_name="saved_search_notifications")
    op.drop_table("saved_search_notifications")
    op.drop_index("ix_saved_searches_user_id", table_name="saved_searches")
    op.drop_table("saved_searches")
