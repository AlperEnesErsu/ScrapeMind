"""keywords.value_en / variants / translated_at — English query expansion

Users type research interests in Turkish; arXiv, Semantic Scholar and PubMed
are English corpora. These columns cache the English form (plus synonyms) of
each global keyword so the scrape can query for both. Filled lazily by
`app.modules.scrape.service.ensure_keyword_translations` — no backfill here,
existing rows translate themselves on the next scan.

Revision ID: e7d3b5a1c9f2
Revises: c3e5f7a9b1d2
Create Date: 2026-07-26 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e7d3b5a1c9f2"
down_revision = "c3e5f7a9b1d2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("keywords", sa.Column("value_en", sa.String(length=128), nullable=True))
    op.add_column("keywords", sa.Column("variants", sa.JSON(), nullable=True))
    op.add_column(
        "keywords", sa.Column("translated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    op.drop_column("keywords", "translated_at")
    op.drop_column("keywords", "variants")
    op.drop_column("keywords", "value_en")
