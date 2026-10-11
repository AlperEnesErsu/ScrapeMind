"""/admin/audit/ through HTTP: who may see it, and what each filter keeps.

test_audit_filter.py checks the queries by hand; it never asked the page.
Every row written here carries a per-test action prefix and every assertion
filters on it, so audit rows other tests leave behind cannot change a count.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from app.core.models.audit import AuditLog
from app.core.models.permission import Permission
from app.core.models.role import Role
from app.core.models.user import User


@pytest.fixture
def viewer(db, auth_client):
    """auth_client's user, holding `audit.view` through an ordinary role."""
    from app.core.rbac.service import invalidate_permission_cache

    client, uid = auth_client
    perm = Permission.query.filter_by(code="audit.view").first()
    if perm is None:
        perm = Permission(code="audit.view", label_key="audit.view")
        db.session.add(perm)
    role = Role(name=f"audit-probe-{uuid.uuid4().hex[:8]}", description="test")
    role.permissions.append(perm)
    db.session.add(role)
    user = db.session.get(User, uid)
    user.is_superuser = False
    user.roles = [role]
    user.locale = "en"  # the headings below are compared with their msgids
    db.session.commit()
    invalidate_permission_cache()

    yield client, uid

    db.session.rollback()
    db.session.execute(text("DELETE FROM user_roles WHERE role_id = :r"), {"r": role.id})
    db.session.execute(text("DELETE FROM role_permissions WHERE role_id = :r"), {"r": role.id})
    db.session.execute(text("DELETE FROM roles WHERE id = :r"), {"r": role.id})
    db.session.commit()
    invalidate_permission_cache()


@pytest.fixture
def tag(db):
    """A unique action prefix; its rows are removed afterwards."""
    prefix = f"probe{uuid.uuid4().hex[:8]}"
    yield prefix
    db.session.rollback()
    db.session.execute(text("DELETE FROM audit_logs WHERE action LIKE :p"), {"p": f"{prefix}%"})
    db.session.commit()


@pytest.fixture
def entries(db, viewer, tag):
    _client, uid = viewer
    rows = [
        AuditLog(
            action=f"{tag}.user.create",
            entity_type="user",
            entity_id="1",
            user_id=uid,
            created_at=datetime(2020, 3, 10, 12, 0),
        ),
        AuditLog(
            action=f"{tag}.user.update",
            entity_type="user",
            entity_id="1",
            user_id=uid,
            created_at=datetime(2020, 3, 12, 12, 0),
        ),
        AuditLog(
            action=f"{tag}.role.create",
            entity_type=f"{tag}-role",
            entity_id="2",
            user_id=None,
            created_at=datetime(2020, 3, 14, 12, 0),
        ),
    ]
    db.session.add_all(rows)
    db.session.commit()
    return uid


def _shown(body: str, tag: str) -> list[str]:
    """The action codes the table lists, in page order."""
    return re.findall(rf'<code class="small text-muted">({tag}[^<]*)</code>', body)


def _get(client, **params) -> str:
    r = client.get("/admin/audit/", query_string=params)
    assert r.status_code == 200
    return r.get_data(as_text=True)


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------


def test_anonymous_is_sent_to_login(client):
    r = client.get("/admin/audit/")
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]


def test_a_user_without_the_permission_is_refused(auth_client, db):
    client, _uid = auth_client
    assert client.get("/admin/audit/").status_code == 403


def test_the_permission_lets_an_ordinary_user_in(viewer):
    client, _uid = viewer
    r = client.get("/admin/audit/")
    assert r.status_code == 200
    assert "Audit Log" in r.get_data(as_text=True)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def test_newest_first_without_filters(viewer, entries, tag):
    client, _uid = viewer
    assert _shown(_get(client, action=tag), tag) == [
        f"{tag}.role.create",
        f"{tag}.user.update",
        f"{tag}.user.create",
    ]


def test_action_is_a_prefix_match(viewer, entries, tag):
    client, _uid = viewer
    assert _shown(_get(client, action=f"{tag}.user"), tag) == [
        f"{tag}.user.update",
        f"{tag}.user.create",
    ]


def test_filter_by_user(viewer, entries, tag):
    client, uid = viewer
    shown = _shown(_get(client, action=tag, user_id=uid), tag)
    assert shown == [f"{tag}.user.update", f"{tag}.user.create"]


def test_filter_by_entity_type(viewer, entries, tag):
    client, _uid = viewer
    body = _get(client, action=tag, entity_type=f"{tag}-role")
    assert _shown(body, tag) == [f"{tag}.role.create"]
    # The dropdown offers it, and keeps it selected.
    assert f'<option value="{tag}-role" selected>' in body


def test_date_range_is_inclusive_of_the_last_day(viewer, entries, tag):
    client, _uid = viewer
    body = _get(client, action=tag, date_from="2020-03-11", date_to="2020-03-12")
    # The 12:00 row on the 12th is in: `date_to` means the end of that day.
    assert _shown(body, tag) == [f"{tag}.user.update"]


def test_only_a_start_date(viewer, entries, tag):
    client, _uid = viewer
    shown = _shown(_get(client, action=tag, date_from="2020-03-13"), tag)
    assert shown == [f"{tag}.role.create"]


def test_an_unreadable_date_is_ignored_not_an_error(viewer, entries, tag):
    client, _uid = viewer
    body = _get(client, action=tag, date_from="2020-13-45", date_to="yesterday")
    assert len(_shown(body, tag)) == 3


def test_no_match_says_so(viewer, tag):
    client, _uid = viewer
    body = _get(client, action=f"{tag}.nothing")
    assert "No entries match." in body


# --------------------------------------------------------------------------
# Pagination and the retention note
# --------------------------------------------------------------------------


def test_fifty_per_page(viewer, tag, db):
    client, _uid = viewer
    db.session.add_all(
        AuditLog(action=f"{tag}.bulk.{i:02d}", created_at=datetime(2021, 1, 1, 0, i))
        for i in range(55)
    )
    db.session.commit()

    first = _shown(_get(client, action=tag), tag)
    second = _shown(_get(client, action=tag, page=2), tag)

    assert len(first) == 50
    assert first[0] == f"{tag}.bulk.54"
    assert second == [f"{tag}.bulk.{i:02d}" for i in range(4, -1, -1)]
    # Past the last page is a 404 (Flask-SQLAlchemy's error_out), not an
    # empty table that looks like "no entries".
    assert client.get("/admin/audit/", query_string={"action": tag, "page": 3}).status_code == 404


def test_the_retention_period_is_shown_when_set(viewer, app, monkeypatch):
    client, _uid = viewer
    monkeypatch.setitem(app.config, "AUDIT_RETENTION_DAYS", 90)
    assert "Retention: 90 days" in _get(client)

    monkeypatch.setitem(app.config, "AUDIT_RETENTION_DAYS", 0)
    assert "Retention:" not in _get(client)
