import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.models import IdentifierType, UserIdentifier
from app.modules.academic.service import (
    add_identifier,
    delete_identifier,
    list_user_identifiers,
    make_email_verify_token,
    verify_email_token,
)


@pytest.fixture
def clean(db):
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
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
            validation_regex=r"^[^@]+@[^@]+\.[^@]+$",
            verification_method="email_link",
        )
    )
    db.session.add(
        IdentifierType(
            code="orcid",
            name="ORCID",
            validation_regex=r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$",
            verification_method=None,
        )
    )
    db.session.commit()
    u = User(
        username="alice",
        email="alice@example.com",
        full_name="Alice",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.query(User).delete()
    db.session.commit()


def test_user_email_cannot_be_added_as_identifier(db, clean):
    ident, err = add_identifier(clean, "email", "alice@example.com")
    assert ident is None
    assert "login email" in err


def test_alternate_email_added_unverified(db, clean):
    ident, err = add_identifier(clean, "email", "Alice.Old@uni.edu")
    assert err is None
    assert ident.value == "alice.old@uni.edu"
    assert ident.is_verified is False


def test_duplicate_alternate_email_rejected(db, clean):
    add_identifier(clean, "email", "second@ex.com")
    ident2, err = add_identifier(clean, "email", "second@ex.com")
    assert ident2 is None
    assert "already registered" in err


def test_orcid_regex_enforced(db, clean):
    bad, err = add_identifier(clean, "orcid", "not-an-orcid")
    assert bad is None
    assert "format" in err

    ok, err = add_identifier(clean, "orcid", "0000-0001-2345-6789")
    assert err is None
    assert ok.value == "0000-0001-2345-6789"


def test_other_users_user_email_blocks_identifier(db, clean):
    other = User(
        username="bob",
        email="bob@example.com",
        full_name="Bob",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(other)
    db.session.commit()
    ident, err = add_identifier(clean, "email", "bob@example.com")
    assert ident is None
    assert "already registered" in err


def test_email_verify_token_roundtrip(db, clean):
    ident, _ = add_identifier(clean, "email", "verify@ex.com")
    token = make_email_verify_token(ident)
    resolved = verify_email_token(token)
    assert resolved is not None
    assert resolved.is_verified is True


def test_email_verify_token_invalid(db, clean, app):
    assert verify_email_token("not-a-token") is None


def test_delete_identifier_ok(db, clean):
    ident, _ = add_identifier(clean, "email", "drop@ex.com")
    ok, err = delete_identifier(clean, ident.id)
    assert ok is True
    assert UserIdentifier.query.get(ident.id) is None


def test_list_filter_by_type(db, clean):
    add_identifier(clean, "email", "a@ex.com")
    add_identifier(clean, "orcid", "0000-0001-2345-6789")
    emails = list_user_identifiers(clean, type_code="email")
    orcids = list_user_identifiers(clean, type_code="orcid")
    assert len(emails) == 1
    assert len(orcids) == 1


def test_verify_email_route_invalid_redirects(client):
    r = client.get("/auth/verify-email/garbage", follow_redirects=False)
    assert r.status_code == 302
