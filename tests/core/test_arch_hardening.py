"""Tests for the architecture-hardening pass.

Covers: password-reset session invalidation (A6) and production config
fail-fast (A3).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.fixture
def user_with_session(db):
    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.session import UserSession
    from app.core.models.user import User

    db.session.query(User).filter_by(username="archtester").delete()
    db.session.commit()
    u = User(
        username="archtester",
        email="archtester@example.test",
        full_name="Arch Tester",
        password_hash=LocalAuthStrategy.hash_password("old-password-123"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    # Two live sessions for this user.
    for _ in range(2):
        db.session.add(UserSession(user_id=u.id, session_key=UserSession.generate_key()))
    db.session.commit()
    yield u
    db.session.execute(text("DELETE FROM user_sessions WHERE user_id = :i"), {"i": u.id})
    db.session.query(User).filter_by(id=u.id).delete()
    db.session.commit()


def test_reset_password_kills_all_sessions(app, user_with_session):
    from app.core.auth.service import reset_password
    from app.core.models.session import UserSession

    with app.test_request_context():
        assert UserSession.query.filter_by(user_id=user_with_session.id).count() == 2
        reset_password(user_with_session, "brand-new-password-456")
        # Every session must be gone — a reset locks out whoever was signed in.
        assert UserSession.query.filter_by(user_id=user_with_session.id).count() == 0


# --------------------------------------------------------------------------
# Production config fail-fast
# --------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, cfg):
        self.config = cfg


def test_production_config_rejects_empty_secret(monkeypatch):
    from app import _validate_production_config

    monkeypatch.setenv("FLASK_ENV", "production")
    with pytest.raises(RuntimeError) as exc:
        _validate_production_config(
            _FakeApp({"SECRET_KEY": "", "SQLALCHEMY_DATABASE_URI": "postgresql://x/y"})
        )
    assert "SECRET_KEY" in str(exc.value)


def test_production_config_rejects_empty_db(monkeypatch):
    from app import _validate_production_config

    monkeypatch.setenv("FLASK_ENV", "production")
    with pytest.raises(RuntimeError) as exc:
        _validate_production_config(
            _FakeApp({"SECRET_KEY": "s3cr3t", "SQLALCHEMY_DATABASE_URI": ""})
        )
    assert "DATABASE_URL" in str(exc.value)


def test_production_config_ok_when_set(monkeypatch):
    from app import _validate_production_config

    monkeypatch.setenv("FLASK_ENV", "production")
    # Should not raise.
    _validate_production_config(
        _FakeApp({"SECRET_KEY": "s3cr3t", "SQLALCHEMY_DATABASE_URI": "postgresql://x/y"})
    )


def test_non_production_config_skips_validation(monkeypatch):
    from app import _validate_production_config

    monkeypatch.setenv("FLASK_ENV", "development")
    # Empty config is fine outside production — no raise.
    _validate_production_config(_FakeApp({"SECRET_KEY": "", "SQLALCHEMY_DATABASE_URI": ""}))
