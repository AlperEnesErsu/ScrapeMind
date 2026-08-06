"""video_summaries table — LLM transcript summary cache for channel videos

Revision ID: b3d9f2a1c4e6
Revises: a3f7c1d92b6e
Create Date: 2026-08-06 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b3d9f2a1c4e6"
down_revision = "a3f7c1d92b6e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "video_summaries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("paper_id", sa.BigInteger(), nullable=False),
        sa.Column("tldr", sa.Text(), nullable=True),
        sa.Column("highlights", sa.JSON(), nullable=True),
        sa.Column("topics", sa.JSON(), nullable=True),
        sa.Column("transcript_chars", sa.Integer(), nullable=True),
        sa.Column("source_lang", sa.String(length=8), nullable=True),
        sa.Column("target_lang", sa.String(length=8), nullable=True),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("paper_id", name="uq_video_summary_paper"),
    )
    op.create_index(
        op.f("ix_video_summaries_deleted_at"), "video_summaries", ["deleted_at"], unique=False
    )


def downgrade():
    op.drop_index(op.f("ix_video_summaries_deleted_at"), table_name="video_summaries")
    op.drop_table("video_summaries")
