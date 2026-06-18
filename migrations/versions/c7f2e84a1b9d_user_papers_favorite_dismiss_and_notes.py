"""user_papers: is_favorite + dismissed_at, plus paper_notes table

Revision ID: c7f2e84a1b9d
Revises: b41e9d27c5a8
Create Date: 2026-06-19 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c7f2e84a1b9d"
down_revision = "b41e9d27c5a8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_papers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_favorite",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_user_papers_is_favorite"), ["is_favorite"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_user_papers_dismissed_at"), ["dismissed_at"], unique=False
        )

    op.create_table(
        "paper_notes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_paper_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tag", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_paper_id"], ["user_papers.id"]),
    )
    with op.batch_alter_table("paper_notes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_notes_user_paper_id"),
            ["user_paper_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_paper_notes_deleted_at"), ["deleted_at"], unique=False
        )


def downgrade():
    with op.batch_alter_table("paper_notes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_notes_deleted_at"))
        batch_op.drop_index(batch_op.f("ix_paper_notes_user_paper_id"))
    op.drop_table("paper_notes")

    with op.batch_alter_table("user_papers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_papers_dismissed_at"))
        batch_op.drop_index(batch_op.f("ix_user_papers_is_favorite"))
        batch_op.drop_column("dismissed_at")
        batch_op.drop_column("is_favorite")
