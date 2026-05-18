import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.models import IdentifierType, UserIdentifier
from app.modules.academic.service import (
    add_email,
    delete_email,
    ensure_primary_email_row,
    list_user_emails,
    make_email_verify_token,
    set_primary_email,
    verify_email_token,
)


@pytest.fixture
def clean(db):
    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()
    db.session.add(
        IdentifierType(
            code="email", name="Email", is_unique_per_user=False, verification_method="email_link"
        )
    )
    db.session.commit()
    u = User(
        username="alice",
        email="alice@ex.com",
        full_name="Alice",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(u)
    db.session.commit()
    ensure_primary_email_row(u)
    yield u
    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()


def test_ensure_primary_creates_row(db, clean):
    rows = list_user_emails(clean)
    assert len(rows) == 1
    assert rows[0].value == "alice@ex.com"
    assert rows[0].is_primary is True
    assert rows[0].is_verified is True


def test_add_email_unverified(db, clean):
    ident, err = add_email(clean, "ALICE+work@ex.com")
    assert err is None
    assert ident.value == "alice+work@ex.com"  # lowered
    assert ident.is_verified is False
    assert ident.is_primary is False


def test_add_email_duplicate_rejected(db, clean):
    add_email(clean, "x@ex.com")
    ident2, err = add_email(clean, "x@ex.com")
    assert ident2 is None
    assert err == "Email already in use."


def test_verify_token_roundtrip(db, clean):
    ident, _ = add_email(clean, "verify@ex.com")
    token = make_email_verify_token(ident)
    resolved = verify_email_token(token)
    assert resolved is not None
    assert resolved.is_verified is True
    assert resolved.verified_at is not None


def test_verify_token_invalid(db, clean, app):
    assert verify_email_token("not-a-token") is None


def test_verify_token_invalidated_on_value_change(db, clean):
    ident, _ = add_email(clean, "rotate@ex.com")
    token = make_email_verify_token(ident)
    ident.value = "different@ex.com"
    db.session.commit()
    assert verify_email_token(token) is None


def test_primary_promotion_requires_verified(db, clean):
    ident, _ = add_email(clean, "unverified@ex.com")
    ok, err = set_primary_email(clean, ident.id)
    assert ok is False
    assert "Verify" in err or "verify" in err


def test_primary_promotion_syncs_user_email(db, clean):
    ident, _ = add_email(clean, "newprimary@ex.com")
    # Manually verify via token roundtrip
    token = make_email_verify_token(ident)
    verify_email_token(token)
    ok, err = set_primary_email(clean, ident.id)
    assert ok is True
    db.session.refresh(clean)
    assert clean.email == "newprimary@ex.com"
    # Old row demoted
    rows = {r.value: r for r in list_user_emails(clean)}
    assert rows["alice@ex.com"].is_primary is False
    assert rows["newprimary@ex.com"].is_primary is True


def test_delete_primary_rejected(db, clean):
    rows = list_user_emails(clean)
    primary = next(r for r in rows if r.is_primary)
    ok, err = delete_email(clean, primary.id)
    assert ok is False
    assert "primary" in err.lower()


def test_delete_secondary_ok(db, clean):
    ident, _ = add_email(clean, "delete-me@ex.com")
    ok, err = delete_email(clean, ident.id)
    assert ok is True
    assert err is None
    assert UserIdentifier.query.get(ident.id) is None


def test_verify_email_route_invalid_redirects(client):
    r = client.get("/auth/verify-email/garbage", follow_redirects=False)
    assert r.status_code == 302
