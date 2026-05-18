from datetime import UTC

import pytest
from sqlalchemy import text

from app.core.auth.service import (
    make_password_reset_token,
    register_user,
    reset_password,
    verify_password_reset_token,
)
from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.role import Role
from app.core.models.user import User


@pytest.fixture
def clean_users(db):
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()
    yield
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()


@pytest.fixture
def user_role(db):
    role = Role.query.filter_by(name="user").first()
    if not role:
        role = Role(name="user", description="default")
        db.session.add(role)
        db.session.commit()
    return role


def test_register_user_happy(db, clean_users, user_role):
    user, err = register_user(
        username="alice", email="ALICE@example.com", full_name="Alice", password="secret12345"
    )
    assert err is None
    assert user is not None
    assert user.email == "alice@example.com"
    assert user.password_hash is not None
    assert LocalAuthStrategy.verify_password("secret12345", user.password_hash)
    assert any(r.name == "user" for r in user.roles)


def test_register_duplicate_username(db, clean_users):
    register_user(username="bob", email="bob@example.com", full_name="Bob", password="x12345678")
    user, err = register_user(
        username="bob", email="bob2@example.com", full_name="Bob", password="x12345678"
    )
    assert user is None
    assert err == "Username already in use."


def test_register_duplicate_email(db, clean_users):
    register_user(
        username="carol", email="carol@example.com", full_name="Carol", password="x12345678"
    )
    user, err = register_user(
        username="carol2", email="carol@example.com", full_name="Carol", password="x12345678"
    )
    assert user is None
    assert err == "Email already in use."


def test_password_reset_token_roundtrip(db, clean_users, app):
    user, _ = register_user(
        username="dave", email="dave@example.com", full_name="Dave", password="x12345678"
    )
    token = make_password_reset_token(user)
    resolved = verify_password_reset_token(token)
    assert resolved is not None
    assert resolved.id == user.id


def test_password_reset_invalid_token(db, clean_users, app):
    assert verify_password_reset_token("garbage.token.here") is None


def test_password_reset_token_invalidated_on_email_change(db, clean_users, app):
    user, _ = register_user(
        username="eve", email="eve@example.com", full_name="Eve", password="x12345678"
    )
    token = make_password_reset_token(user)
    user.email = "eve2@example.com"
    db.session.commit()
    assert verify_password_reset_token(token) is None


def test_reset_password_clears_lockout(db, clean_users, app):
    from datetime import datetime, timedelta

    user, _ = register_user(
        username="frank", email="frank@example.com", full_name="Frank", password="x12345678"
    )
    user.is_locked = True
    user.failed_login_count = 5
    user.locked_until = datetime.now(UTC) + timedelta(minutes=15)
    db.session.commit()
    reset_password(user, "newPass12345")
    assert user.is_locked is False
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert LocalAuthStrategy.verify_password("newPass12345", user.password_hash)


def test_register_route_get(client):
    r = client.get("/auth/register")
    assert r.status_code == 200


def test_forgot_route_get(client):
    r = client.get("/auth/forgot")
    assert r.status_code == 200


def test_reset_route_bad_token(client):
    r = client.get("/auth/reset/garbage", follow_redirects=False)
    assert r.status_code == 302  # redirects to login
