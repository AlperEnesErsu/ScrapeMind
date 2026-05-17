import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.role import Role
from app.core.models.user import User
from app.core.users.service import (
    list_users,
    lock_user,
    set_user_roles,
    soft_delete_user,
    unlock_user,
    update_user,
)


@pytest.fixture
def clean(db):
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.execute(text("DELETE FROM role_permissions"))
    db.session.query(User).delete()
    db.session.query(Role).delete()
    db.session.commit()
    yield
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.execute(text("DELETE FROM role_permissions"))
    db.session.query(User).delete()
    db.session.query(Role).delete()
    db.session.commit()


def _mk_user(db, **over):
    defaults = dict(
        username="alice", email="alice@example.com", full_name="Alice",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    defaults.update(over)
    u = User(**defaults)
    db.session.add(u)
    db.session.commit()
    return u


def test_list_users_search(db, clean):
    _mk_user(db, username="alice", email="a@ex.com", full_name="Alice Smith")
    _mk_user(db, username="bob", email="b@ex.com", full_name="Bob Jones")
    results = list_users(query="ali")
    assert [u.username for u in results] == ["alice"]


def test_list_users_only_active(db, clean):
    _mk_user(db, username="alice", email="a@ex.com", full_name="Alice", is_active=True)
    _mk_user(db, username="bob", email="b@ex.com", full_name="Bob", is_active=False)
    actives = list_users(only_active=True)
    assert {u.username for u in actives} == {"alice"}


def test_update_user_email_collision(db, clean):
    u1 = _mk_user(db, username="alice", email="a@ex.com", full_name="A")
    _mk_user(db, username="bob", email="b@ex.com", full_name="B")
    ok, err = update_user(u1, full_name="A2", email="b@ex.com",
                          is_active=True, is_superuser=False, avatar_url=None)
    assert ok is False
    assert err == "Email already in use."


def test_set_roles_replaces(db, clean):
    u = _mk_user(db)
    r1 = Role(name="r1"); r2 = Role(name="r2")
    db.session.add_all([r1, r2]); db.session.commit()
    set_user_roles(u, [r1.id])
    assert {r.name for r in u.roles} == {"r1"}
    set_user_roles(u, [r2.id])
    assert {r.name for r in u.roles} == {"r2"}
    set_user_roles(u, [])
    assert u.roles == []


def test_lock_unlock_flow(db, clean):
    u = _mk_user(db)
    lock_user(u)
    assert u.is_locked is True
    unlock_user(u)
    assert u.is_locked is False
    assert u.failed_login_count == 0


def test_soft_delete_excludes_from_list(db, clean):
    u = _mk_user(db)
    soft_delete_user(u)
    assert u.is_active is False
    assert all(x.id != u.id for x in list_users())


def test_user_list_route_requires_login(client):
    r = client.get("/admin/users/", follow_redirects=False)
    assert r.status_code in (302, 401)
