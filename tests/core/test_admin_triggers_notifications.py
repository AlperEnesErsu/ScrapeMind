"""Admin manual task triggers + notification center."""

import pytest
from sqlalchemy import text

from app.core.models.notification import Notification, add_notification
from app.core.models.user import User


@pytest.fixture
def admin(db):
    from app.core.auth.strategies.local import LocalAuthStrategy

    db.session.execute(text("DELETE FROM notifications"))
    db.session.query(User).filter_by(username="triggerer").delete()
    db.session.commit()
    u = User(
        username="triggerer",
        email="triggerer@example.test",
        full_name="Triggerer",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
        is_superuser=True,  # bypasses permission_required
    )
    db.session.add(u)
    db.session.commit()
    uid = u.id
    yield u
    db.session.rollback()
    db.session.execute(text("DELETE FROM notifications WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


def _login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True


# ---------------------------------------------------------------------------
# Admin triggers
# ---------------------------------------------------------------------------


def test_trigger_scrape_all_dispatches_task(client, admin, monkeypatch):
    sent = {}

    def fake_send(name, *a, **k):
        sent["name"] = name

        class R:
            id = "task-123"

        return R()

    monkeypatch.setattr("app.tasks.celery_app.send_task", fake_send)
    _login(client, admin.id)
    r = client.post("/admin/tasks/run/scrape_all", follow_redirects=False)
    assert r.status_code == 302
    assert sent["name"] == "scrape.run_for_all_users"


def test_trigger_purge_audit_dispatches_task(client, admin, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "app.tasks.celery_app.send_task",
        lambda name, *a, **k: sent.setdefault("name", name) or type("R", (), {"id": "x"})(),
    )
    _login(client, admin.id)
    client.post("/admin/tasks/run/purge_audit", follow_redirects=False)
    assert sent["name"] == "core.purge_audit_logs"


def test_unknown_trigger_dispatches_nothing(client, admin, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "app.tasks.celery_app.send_task",
        lambda name, *a, **k: sent.setdefault("name", name),
    )
    _login(client, admin.id)
    r = client.post("/admin/tasks/run/rm_rf", follow_redirects=False)
    assert r.status_code == 302  # redirect back, no crash
    assert sent == {}  # arbitrary task name never dispatched


def test_trigger_requires_permission(client, db, monkeypatch):
    """A non-admin without tasks.view can't trigger tasks."""
    from app.core.auth.strategies.local import LocalAuthStrategy

    db.session.query(User).filter_by(username="peon").delete()
    db.session.commit()
    peon = User(
        username="peon",
        email="peon@example.test",
        full_name="Peon",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(peon)
    db.session.commit()
    called = {}
    monkeypatch.setattr("app.tasks.celery_app.send_task", lambda *a, **k: called.setdefault("x", 1))
    _login(client, peon.id)
    r = client.post("/admin/tasks/run/scrape_all")
    assert r.status_code == 403
    assert called == {}
    db.session.query(User).filter_by(username="peon").delete()
    db.session.commit()


def test_trigger_requires_login(client):
    r = client.post("/admin/tasks/run/scrape_all", follow_redirects=False)
    assert r.status_code in (302, 401, 403)


# ---------------------------------------------------------------------------
# Notification center
# ---------------------------------------------------------------------------


def test_all_notifications_page_lists_history(client, admin, db):
    for i in range(3):
        add_notification(admin.id, f"Title {i}", f"Message {i}")
    _login(client, admin.id)
    r = client.get("/notifications/all")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Title 0" in body and "Title 2" in body


def test_all_notifications_does_not_mark_read(client, admin, db):
    """Unlike the dropdown, opening the full page must not clear unread state."""
    add_notification(admin.id, "Unread", "still unread")
    _login(client, admin.id)
    client.get("/notifications/all")
    assert Notification.query.filter_by(user_id=admin.id, is_read=False).count() == 1


def test_mark_all_read(client, admin, db):
    for i in range(3):
        add_notification(admin.id, f"N{i}", "m")
    assert Notification.query.filter_by(user_id=admin.id, is_read=False).count() == 3
    _login(client, admin.id)
    r = client.post("/notifications/read-all", follow_redirects=False)
    assert r.status_code == 302
    assert Notification.query.filter_by(user_id=admin.id, is_read=False).count() == 0


def test_notification_center_is_user_scoped(client, admin, db):
    """Marking read must only touch the caller's own notifications."""
    from app.core.auth.strategies.local import LocalAuthStrategy

    other = User(
        username="other_notif",
        email="other_notif@example.test",
        full_name="Other",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(other)
    db.session.commit()
    add_notification(other.id, "Theirs", "m")

    _login(client, admin.id)
    client.post("/notifications/read-all")
    # The other user's notification is untouched.
    assert Notification.query.filter_by(user_id=other.id, is_read=False).count() == 1
    db.session.execute(text("DELETE FROM notifications WHERE user_id = :uid"), {"uid": other.id})
    db.session.query(User).filter_by(username="other_notif").delete()
    db.session.commit()


def test_notifications_require_login(client):
    assert client.get("/notifications/all", follow_redirects=False).status_code in (302, 401)
    assert client.post("/notifications/read-all", follow_redirects=False).status_code in (
        302,
        401,
    )
