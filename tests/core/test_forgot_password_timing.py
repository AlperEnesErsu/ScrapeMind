"""`/auth/forgot` must not answer with its timing what it refuses to answer
with its text, and a login redirect must speak the visitor's language."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.auth.service import register_user
from app.core.models.user import User


@pytest.fixture
def known_user(db):
    user, err = register_user(
        username="resetter", email="resetter@example.com", full_name="R", password="pw-12345678"
    )
    assert err is None
    yield user
    db.session.execute(text("DELETE FROM user_roles WHERE user_id = :u"), {"u": user.id})
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :u"), {"u": user.id})
    db.session.query(User).filter_by(id=user.id).delete()
    db.session.commit()


@pytest.fixture
def real_mail(app, monkeypatch):
    """Production-like: mail is not suppressed, so nothing is flashed."""
    monkeypatch.setitem(app.config, "MAIL_SUPPRESS_SEND", False)


@pytest.fixture
def recorder(monkeypatch):
    calls = {"inline": [], "queued": []}
    import app.core.email.service as email_service
    from app.tasks import core_tasks

    monkeypatch.setattr(
        email_service,
        "send_password_reset",
        lambda user, url: calls["inline"].append(user.id) or (True, None),
    )
    monkeypatch.setattr(
        core_tasks.send_password_reset_email,
        "delay",
        lambda user_id, url: calls["queued"].append(user_id),
    )
    return calls


def test_known_address_is_queued_not_sent_inside_the_request(
    client, known_user, real_mail, recorder
):
    r = client.post("/auth/forgot", data={"email": "resetter@example.com"})
    assert r.status_code == 302
    assert recorder["queued"] == [known_user.id]
    assert recorder["inline"] == []  # no SMTP round-trip on the known branch


def test_unknown_address_touches_neither_path(client, real_mail, recorder):
    r = client.post("/auth/forgot", data={"email": "nobody@example.com"})
    assert r.status_code == 302
    assert recorder == {"inline": [], "queued": []}


def test_broker_down_still_delivers_inline(client, known_user, real_mail, recorder, monkeypatch):
    from app.tasks import core_tasks

    def refuse(*a, **k):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(core_tasks.send_password_reset_email, "delay", refuse)
    client.post("/auth/forgot", data={"email": "resetter@example.com"})
    assert recorder["inline"] == [known_user.id]


def test_task_sends_to_a_live_user_only(app, known_user, monkeypatch):
    import app.core.email.service as email_service
    from app.tasks.core_tasks import send_password_reset_email

    sent = []
    monkeypatch.setattr(
        email_service, "send_password_reset", lambda u, url: sent.append(u.id) or (True, None)
    )
    assert send_password_reset_email(known_user.id, "https://example.test/r/x") is True
    assert send_password_reset_email(987654321, "https://example.test/r/x") is False
    assert sent == [known_user.id]


def test_reset_task_is_routed_to_io():
    from app.tasks import TASK_ROUTES

    assert TASK_ROUTES["core.send_password_reset"] == {"queue": "io"}


@pytest.mark.parametrize(
    "lang, expected",
    [
        ("tr", "Bu sayfaya erişmek için lütfen giriş yapın."),
        ("en", "Please log in to access this page."),
    ],
)
def test_login_redirect_message_is_in_the_visitors_language(client, lang, expected):
    r = client.get(f"/papers/?lang={lang}", follow_redirects=True)
    assert "/auth/login" in r.request.path
    assert expected in r.get_data(as_text=True)
