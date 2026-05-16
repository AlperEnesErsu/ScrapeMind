import pytest
from sqlalchemy import text

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
