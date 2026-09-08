"""feat(scrape): add user_bluesky table for Bluesky accounts

Revision ID: eb3c1118d2f5
Revises: 08f12848f0d1
Create Date: 2026-09-08 14:02:30.564004

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'eb3c1118d2f5'
down_revision = '08f12848f0d1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_bluesky',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('did', sa.String(length=128), nullable=False),
        sa.Column('handle', sa.String(length=128), nullable=False),
        sa.Column('display_name', sa.String(length=256), nullable=True),
        sa.Column('avatar_url', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('last_post_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'did', name='uq_user_bluesky')
    )
    with op.batch_alter_table('user_bluesky', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_bluesky_deleted_at'), ['deleted_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_bluesky_user_id'), ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('user_bluesky', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_bluesky_user_id'))
        batch_op.drop_index(batch_op.f('ix_user_bluesky_deleted_at'))

    op.drop_table('user_bluesky')
