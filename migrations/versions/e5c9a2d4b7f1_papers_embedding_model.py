"""papers record which model produced their vector

Revision ID: e5c9a2d4b7f1
Revises: d2f6b8c31a57
Create Date: 2026-09-14 10:30:00.000000

Cosine distance between vectors from two embedding models is a number, not a
measurement. If EMBEDDING_MODEL changes, every stored paper vector keeps
returning nearest neighbours -- in library search, the discover feed, "similar
papers" and RAG chat -- as confidently as before and meaning nothing, with no
error anywhere. Patent chunks got this column in `d2f6b8c31a57`; papers had
the same exposure and no guard.

**The backfill is an assumption, stated so it can be undone.** Existing
vectors carry no record of their model. Left NULL, every one of them would be
treated as stale the moment this runs: semantic search would silently go
empty for every user, and the pending task would re-embed the whole library at
provider cost. So existing vectors are stamped with the model the deployment
is configured with *now* (EMBEDDING_MODEL / EMBEDDING_DIM, same defaults as
`app/config.py`). That is correct unless the model was changed at some point
without re-embedding -- in which case those vectors were already wrong, and
the fix is to clear the stamp so they are redone:

    UPDATE papers SET embedding_model = NULL WHERE embedding IS NOT NULL;
"""

import os

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e5c9a2d4b7f1"
down_revision = "d2f6b8c31a57"
branch_labels = None
depends_on = None


def _configured_model() -> str:
    model = (os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small").strip()
    dim = int(os.getenv("EMBEDDING_DIM") or "1536")
    return f"{model}@{dim}"


def upgrade():
    op.add_column("papers", sa.Column("embedding_model", sa.String(length=96), nullable=True))
    op.create_index("ix_papers_embedding_model", "papers", ["embedding_model"])
    op.execute(
        sa.text(
            "UPDATE papers SET embedding_model = :model WHERE embedding IS NOT NULL"
        ).bindparams(model=_configured_model())
    )


def downgrade():
    op.drop_index("ix_papers_embedding_model", table_name="papers")
    op.drop_column("papers", "embedding_model")
