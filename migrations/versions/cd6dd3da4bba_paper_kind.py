"""papers.kind — distinguishes RSS/industry-announcement rows ("news") from
academic papers (NULL, treated as "paper")

Revision ID: cd6dd3da4bba
Revises: d4a8c1f29b3e
Create Date: 2026-07-25 06:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "cd6dd3da4bba"
down_revision = "d4a8c1f29b3e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("papers", sa.Column("kind", sa.String(length=16), nullable=True))


def downgrade():
    op.drop_column("papers", "kind")
