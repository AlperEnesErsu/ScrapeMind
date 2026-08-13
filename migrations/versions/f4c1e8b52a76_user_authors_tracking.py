"""user_authors: openalex_id, orcid, last_work_at, active

Revision ID: f4c1e8b52a76
Revises: e2b7c94d3f18
Create Date: 2026-08-12 21:00:00.000000

Extends the existing table rather than replacing it — `user_authors` has been
there since PR #4-#6 and Faz 5.4 is what finally reads it. Existing rows stay
valid: every new column is nullable, except `active`, which gets a
server_default so already-stored follows come back switched on.

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f4c1e8b52a76"
down_revision = "e2b7c94d3f18"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_authors", sa.Column("openalex_id", sa.String(length=32), nullable=True))
    op.add_column("user_authors", sa.Column("orcid", sa.String(length=19), nullable=True))
    op.add_column(
        "user_authors", sa.Column("last_work_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "user_authors",
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
    )
    # The nightly run resolves follows by OpenAlex id, not by name.
    op.create_index(
        op.f("ix_user_authors_openalex_id"), "user_authors", ["openalex_id"], unique=False
    )


def downgrade():
    op.drop_index(op.f("ix_user_authors_openalex_id"), table_name="user_authors")
    op.drop_column("user_authors", "active")
    op.drop_column("user_authors", "last_work_at")
    op.drop_column("user_authors", "orcid")
    op.drop_column("user_authors", "openalex_id")
