"""Patent loading admin panel (Faz 8.3b).

The load-bearing properties: a plain user cannot reach it, the browser can
only name whitelisted actions (never a task), and a panel with no worker
online says so instead of letting a queued load pass for a running one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.patent.models import PatentIngestRun


@pytest.fixture
def manager(db):
    db.session.query(User).filter_by(username="patentadmin").delete()
    db.session.commit()
    user = User(
        username="patentadmin",
        email="patentadmin@example.test",
        full_name="Patent Admin",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
        is_superuser=True,  # bypasses permission_required, the one place it may
    )
    db.session.add(user)
    db.session.commit()
    uid = user.id
    yield user
    db.session.rollback()
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    PatentIngestRun.query.delete()
    db.session.commit()


def _login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True


def _fake_send(sent):
    def send(name, *a, **k):
        sent.append(name)
        return type("R", (), {"id": "task-1"})()

    return send


def _health(worker):
    return lambda: {"worker": worker}


def test_a_plain_user_is_refused(auth_client):
    client, _ = auth_client
    assert client.get("/patents/admin").status_code == 403
    assert client.post("/patents/admin/run/refresh").status_code == 403


def test_panel_renders_on_an_empty_window(client, manager, monkeypatch):
    monkeypatch.setattr("app.core.health.system_health", _health("ok"))
    _login(client, manager.id)
    body = client.get("/patents/admin").get_data(as_text=True)
    assert "Henüz yükleme çalışmadı." in body


def test_without_a_key_the_panel_says_so_and_only_cleanup_is_offered(client, manager, monkeypatch):
    """No keyless route exists any more, so a load button that could only fail
    must not be pressable -- and the reason must be on the page."""
    import re

    monkeypatch.setattr("app.core.health.system_health", _health("ok"))
    monkeypatch.delenv("USPTO_ODP_API_KEY", raising=False)
    monkeypatch.setitem(client.application.config, "USPTO_ODP_API_KEY", "")
    _login(client, manager.id)
    body = client.get("/patents/admin").get_data(as_text=True)
    assert "USPTO_ODP_API_KEY" in body
    load = re.search(r"<button[^>]*btn-primary[^>]*>", body).group(0)
    purge = re.search(r"<button[^>]*btn-outline-danger[^>]*>", body).group(0)
    assert "disabled" in load
    assert "disabled" not in purge


def test_no_worker_is_stated_and_the_buttons_are_disabled(client, manager, monkeypatch):
    monkeypatch.setattr("app.core.health.system_health", _health("down"))
    _login(client, manager.id)
    body = client.get("/patents/admin").get_data(as_text=True)
    assert "Çevrimiçi worker yok." in body
    assert body.count("disabled") >= 2


def test_recent_runs_are_listed_with_their_error(client, manager, db, monkeypatch):
    monkeypatch.setattr("app.core.health.system_health", _health("ok"))
    db.session.add(
        PatentIngestRun(
            source_file="ipg260908.zip",
            status="error",
            documents_seen=0,
            documents_kept=0,
            error="Could not resolve host",
        )
    )
    db.session.commit()
    _login(client, manager.id)
    body = client.get("/patents/admin").get_data(as_text=True)
    assert "ipg260908.zip" in body
    assert "Could not resolve host" in body


def test_refresh_is_refused_server_side_without_a_key(client, manager, monkeypatch):
    """A disabled button is a hint, not a control: the route must not queue a
    load that can only be skipped, and then report it as queued."""
    sent = []
    monkeypatch.setattr("app.tasks.celery_app.send_task", _fake_send(sent))
    monkeypatch.delenv("USPTO_ODP_API_KEY", raising=False)
    monkeypatch.setitem(client.application.config, "USPTO_ODP_API_KEY", "")
    _login(client, manager.id)
    body = client.post("/patents/admin/run/refresh", follow_redirects=True).get_data(as_text=True)
    assert sent == []
    assert "kuyruğa alındı" not in body


@pytest.mark.parametrize(
    "action,task",
    [("refresh", "patents_bulk.refresh_window"), ("purge", "patents_bulk.purge_window")],
)
def test_actions_dispatch_their_whitelisted_task(client, manager, monkeypatch, action, task):
    sent = []
    monkeypatch.setattr("app.tasks.celery_app.send_task", _fake_send(sent))
    monkeypatch.setitem(client.application.config, "USPTO_ODP_API_KEY", "k")
    _login(client, manager.id)
    resp = client.post(f"/patents/admin/run/{action}", follow_redirects=False)
    assert resp.status_code == 302
    assert sent == [task]


def test_an_unknown_action_dispatches_nothing(client, manager, monkeypatch):
    """The browser names an action, never a task name."""
    sent = []
    monkeypatch.setattr("app.tasks.celery_app.send_task", _fake_send(sent))
    _login(client, manager.id)
    client.post("/patents/admin/run/patents_bulk.refresh_window")
    client.post("/patents/admin/run/core.purge_audit_logs")
    assert sent == []


def test_a_manual_trigger_is_audited(client, manager, db, monkeypatch):
    monkeypatch.setattr("app.tasks.celery_app.send_task", _fake_send([]))
    monkeypatch.setitem(client.application.config, "USPTO_ODP_API_KEY", "k")
    _login(client, manager.id)
    client.post("/patents/admin/run/refresh")
    action = db.session.execute(
        text("SELECT action FROM audit_logs WHERE user_id = :uid ORDER BY id DESC LIMIT 1"),
        {"uid": manager.id},
    ).scalar()
    assert action == "patent.manual_trigger"
