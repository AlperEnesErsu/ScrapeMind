"""fix menu ordering: dashboard_root must sort before discover

Revision ID: b8f3a6d2e4c1
Revises: a1b2c3d4e5f6
Create Date: 2026-07-25 21:00:00.000000

`dashboard_root` (Senin İçin) and `discover` (Keşfet) both seeded with
order_index=10, so the top-level menu tiebreaks arbitrarily and "Keşfet"
sometimes sorts before "Senin İçin". Bump `discover` to 15 — strictly
between `dashboard_root` (10) and `library_root` (20), which stays ahead
of `admin_group` (80) and `settings_profile` (90).

Idempotent (re-running just re-applies the same value) and reversible.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b8f3a6d2e4c1"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    menu = sa.table(
        "menu_items",
        sa.column("code", sa.String()),
        sa.column("order_index", sa.Integer()),
    )
    bind.execute(menu.update().where(menu.c.code == "discover").values(order_index=15))


def downgrade():
    bind = op.get_bind()
    menu = sa.table(
        "menu_items",
        sa.column("code", sa.String()),
        sa.column("order_index", sa.Integer()),
    )
    bind.execute(menu.update().where(menu.c.code == "discover").values(order_index=10))
