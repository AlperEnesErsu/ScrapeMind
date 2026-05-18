import pytest

from app.core.models.permission import Permission
from app.core.models.role import Role
from app.core.rbac.service import (
    create_role,
    get_role,
    list_permissions_by_module,
    list_roles,
    soft_delete_role,
    update_role,
)


@pytest.fixture
def perms(db):
    from sqlalchemy import text

    db.session.execute(text("DELETE FROM role_permissions"))
    db.session.query(Role).delete()
    db.session.query(Permission).delete()
    db.session.commit()
    p1 = Permission(code="test.read", label_key="perm.test.read")
    p2 = Permission(code="test.write", label_key="perm.test.write", module_code=None)
    db.session.add_all([p1, p2])
    db.session.commit()
    yield [p1, p2]
    db.session.execute(text("DELETE FROM role_permissions"))
    db.session.query(Role).delete()
    db.session.query(Permission).delete()
    db.session.commit()


def test_create_role_with_permissions(db, perms):
    role = create_role("editor", "Test role", [perms[0].id, perms[1].id])
    assert role.id is not None
    assert role.name == "editor"
    assert len(role.permissions) == 2


def test_update_role_replaces_permissions(db, perms):
    role = create_role("editor", None, [perms[0].id])
    update_role(role, "editor2", "updated", [perms[1].id])
    fresh = get_role(role.id)
    assert fresh.name == "editor2"
    assert fresh.description == "updated"
    assert {p.code for p in fresh.permissions} == {"test.write"}


def test_soft_delete_excludes_from_list(db, perms):
    role = create_role("temp", None, [])
    assert any(r.id == role.id for r in list_roles())
    soft_delete_role(role)
    assert all(r.id != role.id for r in list_roles())
    assert get_role(role.id) is None


def test_list_permissions_grouped_by_module(db, perms):
    grouped = list_permissions_by_module()
    assert "core" in grouped
    assert {p.code for p in grouped["core"]} == {"test.read", "test.write"}


def test_role_list_route_requires_login(client):
    r = client.get("/admin/rbac/roles", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_role_new_route_requires_login(client):
    r = client.get("/admin/rbac/roles/new", follow_redirects=False)
    assert r.status_code in (302, 401)
