import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.core.settings.service import (
    change_password,
    get_theme,
    update_email,
    update_personal_info,
    update_preferences,
)


@pytest.fixture
def user(db):
    from app.modules.academic.models import IdentifierType
    from app.modules.academic.service import ensure_primary_email_row

    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()
    db.session.add(
        IdentifierType(
            code="email",
            name="Email",
            is_unique_per_user=False,
            verification_method="email_link",
        )
    )
    db.session.commit()
    u = User(
        username="alice",
        email="alice@example.com",
        full_name="Alice",
        password_hash=LocalAuthStrategy.hash_password("oldpass12"),
        locale="tr",
        timezone="Europe/Istanbul",
    )
    db.session.add(u)
    db.session.commit()
    ensure_primary_email_row(u)
    yield u
    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()


def test_update_personal_info(db, user):
    update_personal_info(user, "Alice Smith", "https://example.com/a.png")
    assert user.full_name == "Alice Smith"
    assert user.avatar_url == "https://example.com/a.png"


def test_update_email_with_correct_password(db, user):
    ok, err = update_email(user, "ALICE2@example.com", "oldpass12")
    assert ok is True
    assert err is None
    assert user.email == "alice2@example.com"


def test_update_email_wrong_password(db, user):
    ok, err = update_email(user, "x@example.com", "wrongpass")
    assert ok is False
    assert err == "Current password is incorrect."


def test_update_email_duplicate(db, user):
    other = User(
        username="bob",
        email="bob@example.com",
        full_name="Bob",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(other)
    db.session.commit()
    ok, err = update_email(user, "bob@example.com", "oldpass12")
    assert ok is False
    assert err == "Email already in use."


def test_change_password_happy(db, user):
    ok, err = change_password(user, "oldpass12", "newpass123")
    assert ok is True
    assert err is None
    assert LocalAuthStrategy.verify_password("newpass123", user.password_hash)


def test_change_password_wrong_current(db, user):
    ok, err = change_password(user, "WRONG", "newpass123")
    assert ok is False
    assert err == "Current password is incorrect."


def test_update_preferences_creates_settings(db, user):
    assert user.settings is None
    update_preferences(user, "en", "UTC", "dark")
    assert user.locale == "en"
    assert user.timezone == "UTC"
    db.session.refresh(user)
    assert user.settings is not None
    assert user.settings.settings["theme"] == "dark"
    assert get_theme(user) == "dark"


def test_get_theme_defaults_light(db, user):
    assert get_theme(user) == "light"


def test_profile_route_requires_login(client):
    r = client.get("/settings/profile", follow_redirects=False)
    assert r.status_code in (302, 401)
