"""feat(scrape): add pgvector extension and papers embedding column

Revision ID: f135d2517c0e
Revises: eb3c1118d2f5
Create Date: 2026-09-09 11:37:06.032088

"""
from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f135d2517c0e'
down_revision = 'eb3c1118d2f5'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Ensure pgvector extension is created in PostgreSQL
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    # 2. Add embedding column and HNSW cosine distance index
    with op.batch_alter_table('papers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('embedding', Vector(1536), nullable=True))
        batch_op.create_index(
            'ix_papers_embedding_hnsw',
            ['embedding'],
            unique=False,
            postgresql_using='hnsw',
            postgresql_ops={'embedding': 'vector_cosine_ops'},
        )


def downgrade():
    with op.batch_alter_table('papers', schema=None) as batch_op:
        batch_op.drop_index(
            'ix_papers_embedding_hnsw',
            postgresql_using='hnsw',
            postgresql_ops={'embedding': 'vector_cosine_ops'},
        )
        batch_op.drop_column('embedding')
