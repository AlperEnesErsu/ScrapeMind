"""TOTP service tests — secret lifecycle + recovery code consumption.

We don't drive the full login flow through HTTP here; that's covered by a
route-level test elsewhere. The point of these tests is to keep the service
honest in isolation: real pyotp verification, real argon2 hashing, no mocks.
"""

from types import SimpleNamespace

import pyotp
import pytest

from app.core.auth.totp_service import (
    consume_recovery_code,
    generate_recovery_codes,
    generate_secret,
    provisioning_uri,
    qr_data_uri,
    verify_totp,
)


def test_generate_secret_is_base32_and_long_enough():
    s = generate_secret()
    assert len(s) >= 16
    # base32 alphabet
    assert set(s).issubset(set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"))


def test_verify_totp_accepts_current_code():
    s = generate_secret()
    code = pyotp.TOTP(s).now()
    assert verify_totp(s, code) is True


def test_verify_totp_rejects_wrong_or_malformed():
    s = generate_secret()
    assert verify_totp(s, "000000") is False  # almost certainly not current
    assert verify_totp(s, "abc") is False
    assert verify_totp(s, "") is False
    assert verify_totp("", "123456") is False


def test_provisioning_uri_uses_issuer():
    s = generate_secret()
    uri = provisioning_uri(s, "alice@example.test")
    assert uri.startswith("otpauth://totp/")
    assert "ScrapeMind" in uri
    assert "alice%40example.test" in uri or "alice@example.test" in uri


def test_qr_data_uri_is_png_base64():
    s = generate_secret()
    uri = qr_data_uri(provisioning_uri(s, "alice@example.test"))
    assert uri.startswith("data:image/png;base64,")
    assert len(uri) > 200  # a real QR is well over this


def test_generate_recovery_codes_format_and_uniqueness():
    plain, hashed = generate_recovery_codes()
    assert len(plain) == 8
    assert len(hashed) == 8
    assert len(set(plain)) == 8  # vanishingly unlikely collision
    for c in plain:
        assert len(c) == 9 and c[4] == "-"  # XXXX-XXXX
        assert c.replace("-", "").isalnum()


def test_consume_recovery_code_pops_on_success(db, app):
    """End-to-end against the User model: a real code verifies once and
    only once, and the hash is removed from the row."""
    from sqlalchemy import text

    from app.core.models.role import Role
    from app.core.models.user import User

    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).filter_by(username="totp_test").delete()
    db.session.commit()

    plain, hashed = generate_recovery_codes()
    user = User(
        username="totp_test",
        email="totp_test@example.test",
        full_name="TOTP Test",
        password_hash="x",
        totp_recovery_codes=hashed,
    )
    db.session.add(user)
    db.session.commit()

    assert consume_recovery_code(user, plain[0]) is True
    db.session.refresh(user)
    assert len(user.totp_recovery_codes) == 7
    # Same code can't be used again
    assert consume_recovery_code(user, plain[0]) is False
    # Wrong code rejected
    assert consume_recovery_code(user, "ZZZZ-ZZZZ") is False

    db.session.query(User).filter_by(username="totp_test").delete()
    db.session.commit()


def test_consume_recovery_code_handles_empty_list():
    user = SimpleNamespace(totp_recovery_codes=None)
    assert consume_recovery_code(user, "ABCD-EFGH") is False
    user = SimpleNamespace(totp_recovery_codes=[])
    assert consume_recovery_code(user, "ABCD-EFGH") is False
