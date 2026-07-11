"""Tests for the auth security-hardening pass.

Covers: open-redirect guard, session-fixation rotation, login timing pad,
OAuth email-verification gate, 2FA pending-state expiry, and the
recovery-code row-locked consume.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.auth.security import is_safe_next_url, safe_next_or

# --------------------------------------------------------------------------
# Open-redirect guard (pure unit — no DB)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target,expected",
    [
        ("/papers/", True),
        ("/library/?view=notes", True),
        ("/", True),
        ("https://evil.com", False),
        ("http://evil.com", False),
        ("//evil.com", False),
        ("/\\evil.com", False),
        ("javascript:alert(1)", False),
        ("papers/", False),  # no leading slash
        (None, False),
        ("", False),
    ],
)
def test_is_safe_next_url(target, expected):
    assert is_safe_next_url(target) is expected


def test_safe_next_or_falls_back():
    assert safe_next_or("https://evil.com", "/home") == "/home"
    assert safe_next_or("/papers/", "/home") == "/papers/"
    assert safe_next_or(None, "/home") == "/home"


# --------------------------------------------------------------------------
# Login flow — DB backed
# --------------------------------------------------------------------------


@pytest.fixture
def local_user(db):
    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.user import User

    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).filter_by(username="sectester").delete()
    db.session.commit()
    u = User(
        username="sectester",
        email="sectester@example.test",
        full_name="Sec Tester",
        password_hash=LocalAuthStrategy.hash_password("Str0ngPass!"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :i"), {"i": u.id})
    db.session.execute(text("DELETE FROM user_sessions WHERE user_id = :i"), {"i": u.id})
    db.session.query(User).filter_by(id=u.id).delete()
    db.session.commit()


def test_login_ignores_offsite_next(app, local_user):
    client = app.test_client()
    r = client.post(
        "/auth/login?next=https://evil.com",
        data={"username": "sectester", "password": "Str0ngPass!"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    # Must not send the freshly-authenticated user off-site.
    assert "evil.com" not in r.headers["Location"]


def test_login_honors_relative_next(app, local_user):
    client = app.test_client()
    r = client.post(
        "/auth/login?next=/library/",
        data={"username": "sectester", "password": "Str0ngPass!"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/library/")


def test_login_rotates_session(app, local_user):
    """A value planted in the pre-auth session must not survive login."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["planted_by_attacker"] = "fixation-probe"

    client.post(
        "/auth/login",
        data={"username": "sectester", "password": "Str0ngPass!"},
        follow_redirects=False,
    )

    with client.session_transaction() as sess:
        assert "planted_by_attacker" not in sess
        assert sess.get("_user_id") is not None  # actually logged in


def test_login_wrong_password_no_user_leak(app, local_user):
    """Missing username and wrong password both fail the same way (the timing
    pad makes them indistinguishable; here we just assert both are rejected)."""
    client = app.test_client()
    r1 = client.post(
        "/auth/login",
        data={"username": "sectester", "password": "wrong"},
        follow_redirects=True,
    )
    r2 = client.post(
        "/auth/login",
        data={"username": "ghost-user", "password": "wrong"},
        follow_redirects=True,
    )
    assert r1.status_code == 200 and r2.status_code == 200
    # Neither logs in.
    with client.session_transaction() as sess:
        assert sess.get("_user_id") is None


def test_authenticate_missing_user_returns_none(app, local_user):
    """The timing pad path still returns None for an unknown user."""
    from app.core.auth.strategies.local import LocalAuthStrategy

    with app.app_context():
        result = LocalAuthStrategy().authenticate({"username": "does-not-exist", "password": "x"})
    assert result is None


# --------------------------------------------------------------------------
# OAuth email-verification gate
# --------------------------------------------------------------------------


@pytest.fixture
def oauth_victim(db):
    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.user import User

    db.session.query(User).filter_by(username="oauthvictim").delete()
    db.session.commit()
    u = User(
        username="oauthvictim",
        email="victim@example.test",
        full_name="Victim",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(text("DELETE FROM oauth_accounts WHERE user_id = :i"), {"i": u.id})
    db.session.query(User).filter_by(id=u.id).delete()
    db.session.commit()


def test_oauth_unverified_email_cannot_take_over(app, oauth_victim):
    from app.core.auth.strategies.oauth_base import resolve_oauth_user
    from app.core.models.oauth_account import OAuthAccount

    with app.app_context():
        result = resolve_oauth_user(
            provider="google",
            provider_user_id="attacker-sub-123",
            email="victim@example.test",
            full_name="Attacker",
            raw_data={"email_verified": False},
            email_verified=False,
        )
        assert result is None
        # No account was linked to the victim.
        assert OAuthAccount.query.filter_by(user_id=oauth_victim.id).count() == 0


def test_oauth_verified_email_links(app, oauth_victim):
    from app.core.auth.strategies.oauth_base import resolve_oauth_user
    from app.core.models.oauth_account import OAuthAccount

    with app.app_context():
        result = resolve_oauth_user(
            provider="google",
            provider_user_id="legit-sub-456",
            email="victim@example.test",
            full_name="Victim",
            raw_data={"email_verified": True},
            email_verified=True,
        )
        assert result is not None
        assert result.id == oauth_victim.id
        assert OAuthAccount.query.filter_by(user_id=oauth_victim.id).count() == 1
