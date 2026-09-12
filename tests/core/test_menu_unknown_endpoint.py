"""A menu row pointing at an endpoint this app does not have.

Menu rows are data, and data outlives the code that gave it meaning. A module
can be removed, disabled, or simply absent from the branch someone is running.
`url_for` then raises BuildError — and the sidebar renders on every
authenticated page, so one stale row took the whole signed-in application down,
including the admin page for editing menu rows.

Not hypothetical: two branches of this project shared a development database,
one added a row for a module the other did not have, and every authenticated
page on the other branch answered 500 (docs/PRELAUNCH.md O13). Git cannot see
that kind of conflict, because it is not in any file.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.menu.builder import build_menu_for_user
from app.core.models.menu import MenuItem
from app.extensions import db as _db


@pytest.fixture
def a_user(db):
    from app.core.models.user import User

    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"menu-{suffix}",
        email=f"menu-{suffix}@example.test",
        full_name="Menu Tester",
        password_hash="x",
        is_active=True,
        is_superuser=True,
    )
    _db.session.add(user)
    _db.session.commit()
    yield user
    _db.session.delete(user)
    _db.session.commit()


@pytest.fixture
def stale_row(db):
    """A row for a module that is not installed — the patent case, generically."""
    item = MenuItem(
        code=f"ghost-{uuid.uuid4().hex[:8]}",
        label_key="menu.ghost",
        endpoint="a_module_that_is_not_installed.index",
        icon="bi-question",
        order_index=999,
        is_visible=True,
    )
    _db.session.add(item)
    _db.session.commit()
    yield item
    _db.session.delete(item)
    _db.session.commit()


@pytest.fixture
def good_row(db):
    """A row that does resolve.

    The test database is created empty — `create_all()` with no seed — so
    without this there is no reachable item to survive, and "the rest of the
    menu still builds" would be asserting against an empty list either way.
    """
    item = MenuItem(
        code=f"real-{uuid.uuid4().hex[:8]}",
        label_key="menu.dashboard",
        endpoint="dashboard.index",
        icon="bi-speedometer2",
        order_index=1,
        is_visible=True,
    )
    _db.session.add(item)
    _db.session.commit()
    yield item
    _db.session.delete(item)
    _db.session.commit()


def test_a_row_for_a_missing_module_is_skipped(app, a_user, stale_row):
    """The build must survive it, not raise."""
    with app.test_request_context("/"):
        nodes = build_menu_for_user(a_user)

    codes = {n.item.code for n in nodes}
    assert stale_row.code not in codes, "an unreachable row must not reach the template"


def test_the_rest_of_the_menu_still_builds(app, a_user, stale_row, good_row):
    """One bad row must not cost the others — the actual failure was total."""
    with app.test_request_context("/"):
        nodes = build_menu_for_user(a_user)

    codes = {n.item.code for n in nodes}
    assert good_row.code in codes, "a reachable row must survive an unreachable sibling"
    assert stale_row.code not in codes


def test_every_node_carries_a_usable_href(app, a_user, stale_row, good_row):
    """The template now reads `href` instead of calling `url_for` itself, so
    the crash site is gone rather than guarded."""

    def walk(ns):
        for n in ns:
            yield n
            yield from walk(n.children)

    # `href` is read inside the request, because that is where a template reads
    # it. Outside one it raises, and that is correct: a caller building URLs
    # with no request is making a mistake the menu should not paper over.
    with app.test_request_context("/"):
        nodes = build_menu_for_user(a_user)

        for node in walk(nodes):
            assert node.href, f"{node.item.code} has no href"
            assert not node.href.startswith("a_module_that_is_not_installed")


def test_the_menu_still_builds_without_a_request(app, a_user, stale_row, good_row):
    """The contract I nearly broke while fixing this.

    Deciding reachability with `url_for` would have made `build_menu_for_user`
    require a request context, which it never did — an existing test builds a
    menu outside one. Reachability is settled against `view_functions` instead,
    which needs only an app context.
    """
    with app.app_context():
        nodes = build_menu_for_user(a_user)

    codes = {n.item.code for n in nodes}
    assert good_row.code in codes
    assert stale_row.code not in codes


def test_an_authenticated_page_renders_with_a_stale_row(app, db, a_user, stale_row, good_row):
    """The symptom, end to end: this used to be a 500 on every signed-in page."""
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(a_user.id)
        session["_fresh"] = True

    response = client.get("/", follow_redirects=True)

    assert response.status_code == 200
    assert b"a_module_that_is_not_installed" not in response.data
