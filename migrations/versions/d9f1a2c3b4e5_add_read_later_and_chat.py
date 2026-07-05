"""add read_later and chat

Revision ID: d9f1a2c3b4e5
Revises: f3b1a0c2d8e7
Create Date: 2026-07-05 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd9f1a2c3b4e5'
down_revision = 'f3b1a0c2d8e7'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add read_later to user_papers
    op.add_column('user_papers', sa.Column('read_later', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    
    # 2. Create paper_chat_messages
    op.create_table(
        'paper_chat_messages',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('user_paper_id', sa.BigInteger(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_paper_id'], ['user_papers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_paper_chat_messages_user_paper_id'), 'paper_chat_messages', ['user_paper_id'], unique=False)
    op.create_index(op.f('ix_paper_chat_messages_deleted_at'), 'paper_chat_messages', ['deleted_at'], unique=False)

    # 3. Create notifications
    op.create_table(
        'notifications',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('is_read', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notifications_user_id'), 'notifications', ['user_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_notifications_user_id'), table_name='notifications')
    op.drop_table('notifications')
    op.drop_index(op.f('ix_paper_chat_messages_deleted_at'), table_name='paper_chat_messages')
    op.drop_index(op.f('ix_paper_chat_messages_user_paper_id'), table_name='paper_chat_messages')
    op.drop_table('paper_chat_messages')
    op.drop_column('user_papers', 'read_later')
