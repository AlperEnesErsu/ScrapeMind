"""widen papers.external_id to 512 (RSS GUIDs are full URLs)

Revision ID: e5f1a2b3c4d6
Revises: cd6dd3da4bba
Create Date: 2026-07-25 06:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e5f1a2b3c4d6"
down_revision = "cd6dd3da4bba"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "papers",
        "external_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=512),
        existing_nullable=False,
    )


def downgrade():
    op.alter_column(
        "papers",
        "external_id",
        existing_type=sa.String(length=512),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
