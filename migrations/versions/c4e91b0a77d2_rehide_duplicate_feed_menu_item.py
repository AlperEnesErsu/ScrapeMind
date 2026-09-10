"""re-hide the duplicate scrape.feed menu item, and stop it coming back

Revision ID: c4e91b0a77d2
Revises: 7b3ce9d10a45
Create Date: 2026-09-10 17:10:00.000000

Two menu rows point at `scrape.feed`: `discover` (Keşfet, from the scrape
module) and `scrape_feed` (Yayın Akışı, from scripts/seed.py). `MenuNode.
is_active` matches on endpoint, so both rows lit up at once and the sidebar
showed two active entries on /papers/.

Migration a1b2c3d4e5f6 already diagnosed exactly this and hid `scrape_feed`.
It came back anyway: on this database the library children that migration hid
in the same statement are still hidden, while `scrape_feed` is visible again.
A migration runs once against a given database, so anything that re-creates or
re-enables the row afterwards wins -- and `scripts/seed.py` kept listing it,
so every fresh database got it back on the next seed regardless of the
migration being stamped.

The seed no longer creates it, which is the actual fix. This migration repairs
databases that already have the row.

Kept as a soft hide rather than a delete, matching a1b2c3d4e5f6: the row may
carry an admin's own ordering or permission edits, and hiding is reversible
from /admin/menu while a delete is not.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4e91b0a77d2"
down_revision = "7b3ce9d10a45"
branch_labels = None
depends_on = None


def _menu_table():
    return sa.table(
        "menu_items",
        sa.column("code", sa.String()),
        sa.column("is_visible", sa.Boolean()),
    )


def upgrade():
    menu = _menu_table()
    op.get_bind().execute(
        menu.update().where(menu.c.code == "scrape_feed").values(is_visible=False)
    )


def downgrade():
    # Deliberately not restoring visibility. Reversing this would put the
    # sidebar back into the broken state it describes, and the row is still
    # there for anyone who wants it back from /admin/menu.
    pass
