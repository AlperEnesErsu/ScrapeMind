"""seed the Prior art nav item under Discover

Revision ID: d8a1c6e40f27
Revises: c7e4b8d3a915
Create Date: 2026-08-11 21:30:00.000000

Same idempotent shape as f3b1a0c2d8e7_seed_user_nav_items: never overwrite an
existing row, so a re-run (or an install that already added the item by hand)
is a no-op.

The item is visible to everyone. Gating it on a permission would be the wrong
mechanism — whether patents are usable is a deployment question (keys + the
`patents_enabled` opt-in), and the page itself explains that state. A nav item
that disappears based on config is harder to reason about than a page that
tells you why it is empty.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d8a1c6e40f27"
down_revision = "c7e4b8d3a915"
branch_labels = None
depends_on = None

# (code, parent_code, label_key, icon, endpoint, url, order_index, required_permission)
NEW_ITEMS = [
    ("prior_art", None, "menu.prior_art", "bi-award", "scrape.prior_art", None, 15, None),
]


def upgrade():
    bind = op.get_bind()
    menu = sa.table(
        "menu_items",
        sa.column("id", sa.BigInteger()),
        sa.column("parent_id", sa.BigInteger()),
        sa.column("code", sa.String()),
        sa.column("label_key", sa.String()),
        sa.column("icon", sa.String()),
        sa.column("url", sa.String()),
        sa.column("endpoint", sa.String()),
        sa.column("order_index", sa.Integer()),
        sa.column("is_visible", sa.Boolean()),
        sa.column("required_permission", sa.String()),
    )

    code_to_id: dict[str, int] = {}
    for row in bind.execute(sa.text("SELECT id, code FROM menu_items")):
        code_to_id[row.code] = row.id

    for code, parent_code, label_key, icon, endpoint, url, order_index, perm in NEW_ITEMS:
        if code in code_to_id:
            continue  # idempotent — never overwrite an existing menu item
        parent_id = code_to_id.get(parent_code) if parent_code else None
        bind.execute(
            menu.insert().values(
                parent_id=parent_id,
                code=code,
                label_key=label_key,
                icon=icon,
                endpoint=endpoint,
                url=url,
                order_index=order_index,
                is_visible=True,
                required_permission=perm,
            )
        )


def downgrade():
    bind = op.get_bind()
    for code, *_ in reversed(NEW_ITEMS):
        bind.execute(sa.text("DELETE FROM menu_items WHERE code = :code"), {"code": code})
