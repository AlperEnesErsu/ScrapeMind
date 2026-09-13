"""patent chunks record which model produced their vector

Revision ID: d2f6b8c31a57
Revises: a8d3e6f1b2c4
Create Date: 2026-09-13 15:10:00.000000

Cosine distance between vectors from two different embedding models is a
number, not a measurement. If EMBEDDING_MODEL changes, every stored vector
keeps returning nearest neighbours that look as confident as before and mean
nothing -- with no error anywhere to say so.

`embedding_model` ("model@dimension") lets search read only vectors from the
current model and lets `embed_pending` find the rest and redo them. The
papers table has no equivalent; the patent window is small enough (~5k rows)
that re-embedding on a model change is cheap, so there is no reason to start
without it.

Nullable with no backfill: the column arrives together with the first code
that writes vectors, so no existing row has one to describe.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d2f6b8c31a57"
down_revision = "a8d3e6f1b2c4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "patent_chunks", sa.Column("embedding_model", sa.String(length=96), nullable=True)
    )
    op.create_index("ix_patent_chunks_embedding_model", "patent_chunks", ["embedding_model"])


def downgrade():
    op.drop_index("ix_patent_chunks_embedding_model", table_name="patent_chunks")
    op.drop_column("patent_chunks", "embedding_model")
