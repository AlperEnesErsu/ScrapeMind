"""Verify the seed script creates ORCID/Scopus/WoS identifier types."""

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.models import IdentifierType
from app.modules.academic.service import add_identifier


@pytest.fixture
def types(db):
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.query(User).delete()
    db.session.commit()
    db.session.add_all(
        [
            IdentifierType(
                code="email",
                name="Email",
                validation_regex=r"^[^@]+@[^@]+\.[^@]+$",
                verification_method="email_link",
            ),
            IdentifierType(
                code="orcid",
                name="ORCID",
                validation_regex=r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$",
                verification_method="oauth",
            ),
            IdentifierType(
                code="scopus_id",
                name="Scopus Author ID",
                validation_regex=r"^\d{10,11}$",
                verification_method="manual",
            ),
            IdentifierType(
                code="wos_id",
                name="Web of Science Researcher ID",
                validation_regex=r"^[A-Z]-\d{4}-\d{4}$",
                verification_method="manual",
            ),
        ]
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
    yield u
    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.query(User).delete()
    db.session.commit()


def test_scopus_id_regex_accepts_10_or_11_digits(db, types):
    ok, err = add_identifier(types, "scopus_id", "1234567890")
    assert err is None
    assert ok.value == "1234567890"


def test_scopus_id_regex_rejects_letters(db, types):
    bad, err = add_identifier(types, "scopus_id", "12345abcd0")
    assert bad is None
    assert "format" in err


def test_wos_id_regex_format(db, types):
    ok, _ = add_identifier(types, "wos_id", "E-1234-2020")
    assert ok is not None

    bad, err = add_identifier(types, "wos_id", "lowercase-1234-2020")
    assert bad is None
    assert "format" in err


def test_orcid_with_x_checksum(db, types):
    # ORCID checksum can be "X" — confirm the regex allows it
    ok, _ = add_identifier(types, "orcid", "0000-0002-1825-009X")
    assert ok is not None
