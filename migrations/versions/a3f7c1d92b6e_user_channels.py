"""user_channels table — per-user YouTube channel subscriptions

Revision ID: a3f7c1d92b6e
Revises: 1f15ae022f3e
Create Date: 2026-08-06 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a3f7c1d92b6e"
down_revision = "1f15ae022f3e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_channels",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("etag", sa.String(length=256), nullable=True),
        sa.Column("last_modified", sa.String(length=256), nullable=True),
        sa.Column("last_video_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "channel_id", name="uq_user_channel"),
    )
    op.create_index(op.f("ix_user_channels_user_id"), "user_channels", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_user_channels_deleted_at"), "user_channels", ["deleted_at"], unique=False
    )


def downgrade():
    op.drop_index(op.f("ix_user_channels_deleted_at"), table_name="user_channels")
    op.drop_index(op.f("ix_user_channels_user_id"), table_name="user_channels")
    op.drop_table("user_channels")
