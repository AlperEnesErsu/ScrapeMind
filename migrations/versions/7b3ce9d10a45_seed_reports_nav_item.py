"""seed reports nav item

Split out of 4360c046a92e on purpose. That migration creates the Faz 6 tables
and is safe to apply at any time; this one publishes a menu row pointing at
`scrape.reports`, and `app/core/templates/core/_sidebar.html` builds nav links
with a bare `url_for(item.endpoint)` — no guard. So a menu row whose endpoint
does not exist yet turns every authenticated page into a BuildError.

Keeping the seed in its own revision means the schema can land before the
route does without leaving a commit in between where the app is broken:
this revision belongs with the commit that actually adds `scrape.reports`.

Revision ID: 7b3ce9d10a45
Revises: 4360c046a92e
Create Date: 2026-09-05

"""

import sqlalchemy as sa
from alembic import op

revision = "7b3ce9d10a45"
down_revision = "4360c046a92e"
branch_labels = None
depends_on = None


def upgrade():
    # Same idempotent shape as d8a1c6e40f27_seed_prior_art_nav_item: never
    # overwrite an existing row, so a re-run (or an install that already has
    # the item) is a no-op. English msgid — menu translations are supplied by
    # app/core/i18n/menu_translations.py, not touched here.
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

    # (code, parent_code, label_key, icon, endpoint, url, order_index, required_permission)
    new_items = [
        ("reports", None, "menu.reports", "bi-file-earmark-bar-graph", "scrape.reports", None, 16, None),
    ]

    for code, parent_code, label_key, icon, endpoint, url, order_index, perm in new_items:
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
    bind.execute(sa.text("DELETE FROM menu_items WHERE code = :code"), {"code": "reports"})
