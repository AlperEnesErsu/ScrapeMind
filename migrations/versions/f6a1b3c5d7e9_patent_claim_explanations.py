"""cache plain-language readings of patent claims

Revision ID: f6a1b3c5d7e9
Revises: e5c9a2d4b7f1
Create Date: 2026-09-14 12:10:00.000000

One row per (claim, language). The foreign key cascades from `patent_claims`
on purpose: re-ingesting a corrected grant replaces its claims wholesale, and
the cascade removes every reading of the old text in the same statement, so a
cached explanation can never describe a claim that no longer says that.

Parented on `e5c9a2d4b7f1` (papers.embedding_model), not on the patent chain's
own last revision, because that is where main's head will be when this lands --
two migrations on one parent is the multiple-heads failure PR #98 hit.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f6a1b3c5d7e9"
down_revision = "e5c9a2d4b7f1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "patent_claim_explanations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("patent_claim_id", sa.BigInteger(), nullable=False),
        sa.Column("target_lang", sa.String(length=8), nullable=False),
        sa.Column("plain", sa.Text(), nullable=False),
        sa.Column("narrows", sa.Text(), nullable=True),
        sa.Column("terms", sa.JSON(), nullable=True),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["patent_claim_id"], ["patent_claims.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "patent_claim_id", "target_lang", name="uq_patent_claim_explanation_lang"
        ),
    )
    op.create_index(
        "ix_patent_claim_explanations_deleted_at", "patent_claim_explanations", ["deleted_at"]
    )
    op.create_index(
        "ix_patent_claim_explanations_patent_claim_id",
        "patent_claim_explanations",
        ["patent_claim_id"],
    )


def downgrade():
    op.drop_table("patent_claim_explanations")
