"""paper_analyses + paper_translations — Claude API cache tables

Revision ID: e4a92f17b8d6
Revises: c7f2e84a1b9d
Create Date: 2026-06-19 22:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e4a92f17b8d6"
down_revision = "c7f2e84a1b9d"
branch_labels = None
depends_on = None


def _audit_cols():
    """Common BaseModel columns added to every fresh table."""
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade():
    op.create_table(
        "paper_analyses",
        *_audit_cols(),
        sa.Column("paper_id", sa.BigInteger(), nullable=False),
        sa.Column("target_lang", sa.String(length=8), nullable=False, server_default="tr"),
        sa.Column("tldr", sa.Text(), nullable=True),
        sa.Column("method", sa.JSON(), nullable=True),
        sa.Column("findings", sa.JSON(), nullable=True),
        sa.Column("limitations", sa.JSON(), nullable=True),
        sa.Column("personal_relevance", sa.Text(), nullable=True),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"]),
        sa.UniqueConstraint("paper_id", "target_lang", name="uq_paper_analysis_lang"),
    )
    with op.batch_alter_table("paper_analyses", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_paper_analyses_paper_id"), ["paper_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_paper_analyses_deleted_at"), ["deleted_at"], unique=False)

    op.create_table(
        "paper_translations",
        *_audit_cols(),
        sa.Column("paper_id", sa.BigInteger(), nullable=False),
        sa.Column("target_lang", sa.String(length=8), nullable=False),
        sa.Column("title_translated", sa.Text(), nullable=True),
        sa.Column("abstract_translated", sa.Text(), nullable=True),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"]),
        sa.UniqueConstraint("paper_id", "target_lang", name="uq_paper_translation_lang"),
    )
    with op.batch_alter_table("paper_translations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_paper_translations_paper_id"), ["paper_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_paper_translations_deleted_at"), ["deleted_at"], unique=False
        )


def downgrade():
    with op.batch_alter_table("paper_translations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_translations_deleted_at"))
        batch_op.drop_index(batch_op.f("ix_paper_translations_paper_id"))
    op.drop_table("paper_translations")

    with op.batch_alter_table("paper_analyses", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_paper_analyses_deleted_at"))
        batch_op.drop_index(batch_op.f("ix_paper_analyses_paper_id"))
    op.drop_table("paper_analyses")
