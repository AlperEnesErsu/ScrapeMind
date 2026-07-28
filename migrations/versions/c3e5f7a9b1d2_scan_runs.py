"""scan_runs table — real per-user scan history

Backs the "last scan / next scan" line in the UI, which previously guessed
from max(user_papers.created_at) and a hardcoded "03:15" string.

Revision ID: c3e5f7a9b1d2
Revises: b8f3a6d2e4c1
Create Date: 2026-07-26 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c3e5f7a9b1d2"
down_revision = "b8f3a6d2e4c1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="auto"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_scan_runs_user_id"), "scan_runs", ["user_id"], unique=False)
    op.create_index(op.f("ix_scan_runs_deleted_at"), "scan_runs", ["deleted_at"], unique=False)
    # Every read is "this user's runs, newest first" — the composite index
    # serves both the dashboard lookup and the duration-median estimate.
    op.create_index(
        "ix_scan_runs_user_started", "scan_runs", ["user_id", "started_at"], unique=False
    )


def downgrade():
    op.drop_index("ix_scan_runs_user_started", table_name="scan_runs")
    op.drop_index(op.f("ix_scan_runs_deleted_at"), table_name="scan_runs")
    op.drop_index(op.f("ix_scan_runs_user_id"), table_name="scan_runs")
    op.drop_table("scan_runs")
