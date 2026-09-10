"""Invariants the sidebar depends on, checked against a freshly seeded menu.

`MenuNode.is_active` decides highlighting by comparing `request.endpoint` to
the item's endpoint. That is correct, and it is also why two rows pointing at
one endpoint both render active -- the bug reported as "two menu items are
highlighted at once" was never in the comparison, it was in the data.

The menu is seeded rather than migrated into the test database (conftest builds
schema with `create_all`), so these run the seed and assert on what a fresh
install actually gets. A migration cannot protect this: it runs once, while the
seed runs on every new database.
"""

from __future__ import annotations

import collections

import pytest

from app.core.models.menu import MenuItem

# Menu rows reach a real database from two places: `scripts/seed.py`, and
# migrations. `discover` is a migration's (f3b1a0c2d8e7), and the test database
# is built with `create_all` rather than by migrating, so seeding into an empty
# table cannot reproduce the collision that actually happened -- an earlier
# version of this test passed for exactly that reason, and kept passing when
# the duplicate was put back.
#
# So the fixture stands the migration's rows up first, then seeds on top. That
# is the real sequence: `flask db upgrade`, then someone runs the seed.
MIGRATION_OWNED = [
    dict(
        code="discover",
        label_key="menu.discover",
        icon="bi-compass",
        endpoint="scrape.feed",
        order_index=15,
        is_visible=True,
    ),
    dict(
        code="library_root",
        label_key="menu.library",
        icon="bi-journal-bookmark",
        endpoint="library.index",
        order_index=20,
        is_visible=True,
    ),
]


@pytest.fixture
def seeded_menu(db, app):
    """A database that has been migrated, and is then seeded."""
    from scripts.seed import run as run_seed

    db.session.query(MenuItem).delete()
    db.session.commit()
    for row in MIGRATION_OWNED:
        db.session.add(MenuItem(**row))
    db.session.commit()

    run_seed(app)
    return MenuItem.query.all()


def test_seed_gives_no_two_visible_items_the_same_endpoint(seeded_menu):
    """One page, one sidebar entry.

    `scrape_feed` and `discover` both pointed at `scrape.feed`, so /papers/
    highlighted two entries. Migration a1b2c3d4e5f6 hid one; the seed put it
    back on the next fresh database, which is why the fix had to move into the
    seed itself.
    """
    endpoints = [m.endpoint for m in seeded_menu if m.is_visible and m.endpoint]
    duplicates = {ep: n for ep, n in collections.Counter(endpoints).items() if n > 1}
    assert duplicates == {}, (
        "these endpoints have more than one visible menu item, so every one of "
        f"them renders as active at the same time: {duplicates}"
    )


def test_every_visible_item_is_reachable(seeded_menu):
    """A visible row needs an endpoint, a url, or children to be worth showing.

    A row with none of the three renders as a dead link. `_prune_empty_groups`
    drops them at render time, but a seed that creates one is still creating
    something nobody can click.
    """
    by_parent = collections.defaultdict(list)
    for item in seeded_menu:
        by_parent[item.parent_id].append(item)

    dead = [
        item.code
        for item in seeded_menu
        if item.is_visible and not item.endpoint and not item.url and not by_parent[item.id]
    ]
    assert dead == [], f"visible but unclickable and childless: {dead}"


def test_seed_is_idempotent(db, app, seeded_menu):
    """Seeding twice must not double the menu.

    Every block in `scripts/seed.py` guards on `filter_by(code=...)`, and this
    holds it to that -- a block added without the guard shows up here rather
    than as duplicated sidebar entries on someone's second run.
    """
    from scripts.seed import run as run_seed

    before = {m.code for m in seeded_menu}
    run_seed(app)
    after = MenuItem.query.all()
    assert len(after) == len(before), "seeding twice changed the menu row count"
    assert {m.code for m in after} == before
