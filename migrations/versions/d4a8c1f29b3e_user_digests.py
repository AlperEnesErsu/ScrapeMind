"""user_digests table — LLM daily/weekly briefing rows

Revision ID: d4a8c1f29b3e
Revises: b7e2f9a1c3d4
Create Date: 2026-07-25 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d4a8c1f29b3e"
down_revision = "b7e2f9a1c3d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_digests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("highlights", sa.JSON(), nullable=True),
        sa.Column("themes", sa.JSON(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "period", "period_start", name="uq_user_digest_period"),
    )
    op.create_index(op.f("ix_user_digests_user_id"), "user_digests", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_digests_deleted_at"), "user_digests", ["deleted_at"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_user_digests_deleted_at"), table_name="user_digests")
    op.drop_index(op.f("ix_user_digests_user_id"), table_name="user_digests")
    op.drop_table("user_digests")
