"""users: TOTP columns (2FA)

Revision ID: b41e9d27c5a8
Revises: cdf83a408b68
Create Date: 2026-05-26 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b41e9d27c5a8'
down_revision = 'cdf83a408b68'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_secret', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('totp_enabled_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('totp_recovery_codes', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('totp_recovery_codes')
        batch_op.drop_column('totp_enabled_at')
        batch_op.drop_column('totp_secret')
