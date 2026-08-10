"""source_quota_usage table — cumulative weekly budgets for licensed sources

Revision ID: c7e4b8d3a915
Revises: b3d9f2a1c4e6
Create Date: 2026-08-10 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c7e4b8d3a915"
down_revision = "b3d9f2a1c4e6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_quota_usage",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requests_used", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("bytes_used", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # `consume_quota` relies on this constraint by name-free ON CONFLICT
        # over (source_name, window_start): it is what makes "one row per
        # source per week" true under concurrent workers.
        sa.UniqueConstraint("source_name", "window_start", name="uq_source_quota_window"),
    )
    op.create_index(
        op.f("ix_source_quota_usage_deleted_at"),
        "source_quota_usage",
        ["deleted_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_source_quota_usage_deleted_at"), table_name="source_quota_usage")
    op.drop_table("source_quota_usage")
