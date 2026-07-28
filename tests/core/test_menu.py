import pytest
from sqlalchemy import text

from app.core.menu.builder import build_menu_for_user
from app.core.menu.service import (
    create_item,
    get_item,
    hard_delete,
    has_cycle,
    is_critical,
    list_items,
    toggle_visible,
    update_item,
)
from app.core.models.menu import MenuItem
from app.core.models.user import User


@pytest.fixture
def clean_menu(db):
    db.session.execute(text("DELETE FROM role_menus"))
    db.session.query(MenuItem).delete()
    db.session.commit()
    yield
    db.session.execute(text("DELETE FROM role_menus"))
    db.session.query(MenuItem).delete()
    db.session.commit()


def _payload(code: str, **overrides) -> dict:
    base = {
        "code": code,
        "label_key": f"menu.{code}",
        "icon": "bi-square",
        "url": None,
        "endpoint": None,
        "parent_id": 0,
        "module_code": "",
        "required_permission": "",
        "order_index": 10,
        "is_visible": True,
    }
    base.update(overrides)
    return base


def test_create_and_list_items(db, clean_menu):
    item = create_item(_payload("alpha"))
    assert item.id is not None
    assert item.code == "alpha"
    assert any(i.code == "alpha" for i in list_items())


def test_update_preserves_code(db, clean_menu):
    item = create_item(_payload("beta"))
    update_item(item, _payload("beta_renamed", order_index=99, is_visible=False))
    fresh = get_item(item.id)
    assert fresh.code == "beta"  # immutable
    assert fresh.order_index == 99
    assert fresh.is_visible is False


def test_toggle_visible(db, clean_menu):
    item = create_item(_payload("gamma"))
    assert item.is_visible is True
    toggle_visible(item)
    assert get_item(item.id).is_visible is False


def test_hard_delete_removes_item(db, clean_menu):
    item = create_item(_payload("delta"))
    item_id = item.id
    hard_delete(item)
    assert get_item(item_id) is None


def test_cycle_detection_direct(db, clean_menu):
    parent = create_item(_payload("p"))
    child = create_item(_payload("c", parent_id=parent.id))
    # Trying to set parent.parent_id = child.id would cycle
    assert has_cycle(parent.id, child.id) is True
    assert has_cycle(child.id, parent.id) is False  # child→parent is fine


def test_is_critical_known_codes():
    item = MenuItem(code="admin_roles", label_key="menu.roles")
    assert is_critical(item) is True
    item2 = MenuItem(code="random_code", label_key="menu.random")
    assert is_critical(item2) is False


def test_menu_list_route_requires_login(client):
    r = client.get("/admin/menu/", follow_redirects=False)
    assert r.status_code in (302, 401)


@pytest.fixture
def menu_builder_users(db):
    """A regular user (no roles/permissions) and a superuser.

    Not reusing `auth_client` — build_menu_for_user() takes a plain User
    instance and doesn't need a logged-in test client.
    """
    db.session.query(User).filter(User.username.in_(["menu_regular", "menu_super"])).delete(
        synchronize_session=False
    )
    db.session.commit()

    regular = User(
        username="menu_regular",
        email="menu_regular@example.test",
        full_name="Menu Regular",
    )
    superuser = User(
        username="menu_super",
        email="menu_super@example.test",
        full_name="Menu Super",
        is_superuser=True,
    )
    db.session.add_all([regular, superuser])
    db.session.commit()

    yield regular, superuser

    db.session.query(User).filter(User.id.in_([regular.id, superuser.id])).delete(
        synchronize_session=False
    )
    db.session.commit()


def test_build_menu_prunes_empty_admin_group_for_regular_user(db, clean_menu, menu_builder_users):
    """`admin_group` (menu.admin) has no required_permission of its own — its
    children are permission-gated and get filtered out for a user with no
    admin perms, which used to leave a dead, childless accordion header in
    the sidebar. build_menu_for_user() must prune that empty group, while
    still surfacing it (with its children) for a superuser."""
    regular, superuser = menu_builder_users

    admin_group = create_item(
        _payload("admin_group", label_key="menu.admin", endpoint=None, order_index=80)
    )
    create_item(
        _payload(
            "admin_users",
            label_key="menu.users",
            endpoint="users.user_list",
            parent_id=admin_group.id,
            required_permission="users.view",
            order_index=10,
        )
    )
    create_item(
        _payload(
            "dashboard_root",
            label_key="menu.dashboard",
            endpoint="dashboard.index",
            order_index=10,
        )
    )

    regular_tree = build_menu_for_user(regular)
    regular_codes = {node.item.code for node in regular_tree}
    assert "admin_group" not in regular_codes
    assert "dashboard_root" in regular_codes

    superuser_tree = build_menu_for_user(superuser)
    superuser_codes = {node.item.code for node in superuser_tree}
    assert "admin_group" in superuser_codes
    admin_node = next(n for n in superuser_tree if n.item.code == "admin_group")
    assert {c.item.code for c in admin_node.children} == {"admin_users"}
