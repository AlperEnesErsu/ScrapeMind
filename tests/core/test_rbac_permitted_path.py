"""The half of RBAC nothing tested: a user who is *not* a superuser and *does*
hold the permission gets in.

Every existing test reached `permission_required` either as a superuser (the
bypass on its first branch) or without the permission (403). The line that
lets a granted, ordinary user through -- the one every real role depends on --
had never run under test. A regression there (a wrong code, a cache that
returns the empty set) would lock out every non-admin role while the suite
stayed green.

The routes are discovered, not listed: each view's permission code is read
from the decorator's closure, so a new protected page is covered the day it
is added.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.core.models.permission import Permission
from app.core.models.role import Role
from app.core.models.user import User


def _required_permission(view) -> str | None:
    """The code `permission_required` closed over, if this view has one."""
    fn = view
    while fn is not None:
        code = getattr(fn, "__code__", None)
        if code is not None and "permission_code" in code.co_freevars:
            cell = fn.__closure__[code.co_freevars.index("permission_code")]
            return cell.cell_contents
        fn = getattr(fn, "__wrapped__", None)
    return None


def _protected_get_routes(app):
    routes = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods:
            continue
        perm = _required_permission(app.view_functions[rule.endpoint])
        if perm is None:
            continue
        url = rule.rule
        for arg in rule.arguments:
            # An id no row has: the permitted user should reach the view and
            # get its own not-found answer, never the decorator's 403.
            url = re.sub(r"<(?:[^:>]+:)?" + arg + ">", "999999", url)
        routes.append((rule.endpoint, url, perm))
    return sorted(routes)


@pytest.fixture(autouse=True)
def _no_celery_broadcasts(monkeypatch):
    """/admin/tasks asks the broker who is alive; here nobody is, and each
    broadcast would wait out its timeout. Not what this file tests."""
    from app.tasks import celery_app

    class _Silent:
        def __getattr__(self, method):
            return lambda: {}

    monkeypatch.setattr(celery_app.control, "inspect", lambda **kwargs: _Silent())


@pytest.fixture
def role_holder(app, db, auth_client):
    """auth_client's user, not a superuser, given one role per test."""
    client, uid = auth_client
    created = []

    def grant(code: str | None):
        user = db.session.get(User, uid)
        user.is_superuser = False
        user.roles = []
        role = Role(name=f"probe-{len(created)}", description="test")
        if code is not None:
            perm = Permission.query.filter_by(code=code).first()
            if perm is None:
                perm = Permission(code=code, label_key=code)
                db.session.add(perm)
            role.permissions.append(perm)
        db.session.add(role)
        user.roles.append(role)
        db.session.commit()
        created.append(role.id)
        from app.core.rbac.service import invalidate_permission_cache

        invalidate_permission_cache()
        return client

    yield grant

    db.session.rollback()
    for rid in created:
        db.session.execute(text("DELETE FROM user_roles WHERE role_id = :r"), {"r": rid})
        db.session.execute(text("DELETE FROM role_permissions WHERE role_id = :r"), {"r": rid})
        db.session.execute(text("DELETE FROM roles WHERE id = :r"), {"r": rid})
    db.session.commit()


def test_the_discovery_finds_the_protected_pages(app):
    endpoints = {endpoint for endpoint, _url, _perm in _protected_get_routes(app)}
    # A floor, not an inventory: if discovery broke, this would see none.
    assert {
        "users.user_list",
        "audit.log_list",
        "rbac.role_list",
        "menu.menu_list",
        "tasks_admin.overview",
    } <= endpoints, endpoints


def test_every_protected_page_admits_the_permission_and_only_it(app, role_holder):
    routes = _protected_get_routes(app)
    assert routes

    client = role_holder(None)  # a role with no permissions at all
    denied = {url: client.get(url).status_code for _e, url, _p in routes}
    leaks = {url: code for url, code in denied.items() if code != 403}
    assert not leaks, f"reachable without the permission: {leaks}"

    wrong = []
    for endpoint, url, perm in routes:
        client = role_holder(perm)
        code = client.get(url).status_code
        if code == 403 or code >= 500:
            wrong.append((endpoint, url, perm, code))
    assert not wrong, f"permitted non-superuser was refused or crashed: {wrong}"


def test_a_different_permission_does_not_open_the_page(app, role_holder):
    routes = _protected_get_routes(app)
    codes = sorted({perm for _e, _u, perm in routes})
    assert len(codes) >= 2
    for endpoint, url, perm in routes:
        other = next(c for c in codes if c != perm)
        client = role_holder(other)
        assert client.get(url).status_code == 403, (endpoint, perm, other)
