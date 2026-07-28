"""collections — user-named folders of papers

Revision ID: c5e88a1f2d34
Revises: a7c4e91b2d60
Create Date: 2026-07-25 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c5e88a1f2d34"
down_revision = "e7d3b5a1c9f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_collection_user_name"),
    )
    op.create_index("ix_collections_user_id", "collections", ["user_id"])
    op.create_index("ix_collections_deleted_at", "collections", ["deleted_at"])

    op.create_table(
        "collection_papers",
        sa.Column("collection_id", sa.BigInteger(), nullable=False),
        sa.Column("user_paper_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_paper_id"], ["user_papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("collection_id", "user_paper_id"),
    )


def downgrade():
    op.drop_table("collection_papers")
    op.drop_index("ix_collections_deleted_at", table_name="collections")
    op.drop_index("ix_collections_user_id", table_name="collections")
    op.drop_table("collections")
