"""user_feeds table — per-user custom RSS/Atom feeds

Revision ID: f7c4b8e1a2d9
Revises: e5f1a2b3c4d6
Create Date: 2026-07-25 11:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f7c4b8e1a2d9"
down_revision = "e5f1a2b3c4d6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_feeds",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_feeds_user_id"), "user_feeds", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_feeds_deleted_at"), "user_feeds", ["deleted_at"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_user_feeds_deleted_at"), table_name="user_feeds")
    op.drop_index(op.f("ix_user_feeds_user_id"), table_name="user_feeds")
    op.drop_table("user_feeds")
