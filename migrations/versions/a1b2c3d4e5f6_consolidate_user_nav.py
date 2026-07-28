"""consolidate user-side nav: hide duplicate scrape_feed + library children

Revision ID: a1b2c3d4e5f6
Revises: f7c4b8e1a2d9
Create Date: 2026-07-25 12:00:00.000000

Two items pointed at `scrape.feed` (seed's `scrape_feed` / label menu.feed,
and migration f3b1a0c2d8e7's `discover` / label menu.discover) — we keep
`discover` (icon bi-compass) and hide `scrape_feed`.

`library_root` (menu.library) had 3 children (`library_timeline` /
`library_favorites` / `library_notes`) that all just point at `/library/`,
which has its own in-page view tabs — redundant sidebar entries. Consolidate
to the single `library_root` link by hiding the 3 children.

Soft-hide only (is_visible=False), not a hard delete, so this stays
idempotent and reversible.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "f7c4b8e1a2d9"
branch_labels = None
depends_on = None

CODES = ["scrape_feed", "library_timeline", "library_favorites", "library_notes"]


def upgrade():
    bind = op.get_bind()
    menu = sa.table(
        "menu_items",
        sa.column("code", sa.String()),
        sa.column("is_visible", sa.Boolean()),
    )
    bind.execute(menu.update().where(menu.c.code.in_(CODES)).values(is_visible=False))


def downgrade():
    bind = op.get_bind()
    menu = sa.table(
        "menu_items",
        sa.column("code", sa.String()),
        sa.column("is_visible", sa.Boolean()),
    )
    bind.execute(menu.update().where(menu.c.code.in_(CODES)).values(is_visible=True))
