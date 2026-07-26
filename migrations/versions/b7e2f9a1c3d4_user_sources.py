"""user_sources table — per-user scrape source opt-out

Revision ID: b7e2f9a1c3d4
Revises: a7c4e91b2d60
Create Date: 2026-07-24 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b7e2f9a1c3d4"
down_revision = "a7c4e91b2d60"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_sources",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source_name", name="uq_user_source"),
    )
    op.create_index(op.f("ix_user_sources_user_id"), "user_sources", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_sources_deleted_at"), "user_sources", ["deleted_at"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_user_sources_deleted_at"), table_name="user_sources")
    op.drop_index(op.f("ix_user_sources_user_id"), table_name="user_sources")
    op.drop_table("user_sources")
