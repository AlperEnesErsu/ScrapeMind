"""seed user-side menu items: Discover + Library (+children)

Revision ID: f3b1a0c2d8e7
Revises: e4a92f17b8d6
Create Date: 2026-06-19 23:00:00.000000

User-side researcher nav. Existing dashboard_root stays; we add Discover
and the Library tree so /papers/ and /library/ have proper sidebar entries.
Admin-side panels (/admin/*) come from `is_superuser` bypass + the dynamic
permission filter, not from these rows.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f3b1a0c2d8e7"
down_revision = "e4a92f17b8d6"
branch_labels = None
depends_on = None


# (code, parent_code, label_key, icon, endpoint, url, order_index, required_permission)
NEW_ITEMS = [
    # User-side, top level
    ("discover", None, "menu.discover", "bi-compass", "scrape.feed", None, 10, None),
    ("library_root", None, "menu.library", "bi-bookmark-heart", "library.index", None, 20, None),
    # Library children — same view route with ?view= query string. We use
    # the `url` column for these because Flask url_for can't carry query args
    # through a single endpoint name.
    (
        "library_timeline",
        "library_root",
        "menu.library.timeline",
        "bi-clock-history",
        None,
        "/library/",
        1,
        None,
    ),
    (
        "library_favorites",
        "library_root",
        "menu.library.favorites",
        "bi-heart",
        None,
        "/library/?view=favorites",
        2,
        None,
    ),
    (
        "library_notes",
        "library_root",
        "menu.library.notes",
        "bi-journal-text",
        None,
        "/library/?view=notes",
        3,
        None,
    ),
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

    # Two-pass insert so children can resolve their parent_id.
    code_to_id: dict[str, int] = {}
    # Pre-populate with existing codes so re-runs and existing items don't break us.
    for row in bind.execute(sa.text("SELECT id, code FROM menu_items")):
        code_to_id[row.code] = row.id

    for code, parent_code, label_key, icon, endpoint, url, order_index, perm in NEW_ITEMS:
        if code in code_to_id:
            continue  # idempotent — never overwrite an existing menu item
        parent_id = code_to_id.get(parent_code) if parent_code else None
        res = bind.execute(
            menu.insert()
            .values(
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
            .returning(menu.c.id)
        )
        code_to_id[code] = res.scalar()


def downgrade():
    bind = op.get_bind()
    codes = [c for c, *_ in NEW_ITEMS]
    # Delete children first, then parents.
    for code in reversed(codes):
        bind.execute(sa.text("DELETE FROM menu_items WHERE code = :code"), {"code": code})
