"""TOTP (Time-based One-Time Password) — 2FA support.

Three concerns live here:

1. Secret lifecycle — generate a base32 secret, build the otpauth:// URI
   used by authenticator apps, render that URI as a QR data-URI for the
   enable page.
2. Code verification — both the running TOTP (with a one-step window for
   clock drift) and one-shot recovery codes (argon2-hashed).
3. Recovery codes — generate human-friendly XXXX-XXXX codes, store hashes,
   verify-and-consume on success.

No Flask state, no DB writes directly — services return values; routes
own commits. Keeps this layer trivially unit-testable.
"""

from __future__ import annotations

import base64
import io
import secrets
from datetime import UTC, datetime

import pyotp
import qrcode
from passlib.context import CryptContext

from app.core.models.user import User
from app.extensions import db

ISSUER = "ScrapeMind"
RECOVERY_CODE_COUNT = 8
RECOVERY_GROUP_LEN = 4  # XXXX-XXXX → two groups of 4
RECOVERY_GROUPS = 2
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I

# Argon2 is overkill for short OTP codes but we already depend on it and
# uniformity beats inventing a bespoke hasher.
_pwd_ctx = CryptContext(schemes=["argon2"], deprecated="auto")


# --------------------------------------------------------------------------
# Secret + provisioning URI + QR
# --------------------------------------------------------------------------


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account, issuer_name=ISSUER)


def qr_data_uri(otpauth_uri: str) -> str:
    """Render a QR code as a base64 data URI for an <img> tag."""
    img = qrcode.make(otpauth_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------
# Code verification
# --------------------------------------------------------------------------


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    # valid_window=1 → allow the previous and next 30s windows to compensate
    # for small clock drift between the user's phone and our server.
    return pyotp.TOTP(secret).verify(code, valid_window=1)


# --------------------------------------------------------------------------
# Recovery codes
# --------------------------------------------------------------------------


def _format_recovery_code() -> str:
    parts = [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_GROUP_LEN))
        for _ in range(RECOVERY_GROUPS)
    ]
    return "-".join(parts)


def generate_recovery_codes() -> tuple[list[str], list[str]]:
    """Returns (plaintext_codes, hashed_codes). Show plaintext to the user
    once and only once; store hashes on the user row."""
    plain = [_format_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    hashed = [_pwd_ctx.hash(c) for c in plain]
    return plain, hashed


def consume_recovery_code(user: User, code: str) -> bool:
    """Verify and pop a recovery code. Commits on success."""
    codes = list(user.totp_recovery_codes or [])
    if not codes:
        return False
    candidate = code.strip().upper().replace(" ", "")
    if not candidate:
        return False
    for idx, hashed in enumerate(codes):
        try:
            ok = _pwd_ctx.verify(candidate, hashed)
        except Exception:
            ok = False
        if ok:
            codes.pop(idx)
            user.totp_recovery_codes = codes
            db.session.commit()
            return True
    return False


# --------------------------------------------------------------------------
# Lifecycle helpers — used from routes
# --------------------------------------------------------------------------


def enable_totp(user: User, secret: str) -> list[str]:
    """Activate 2FA on this user and return fresh recovery codes (plaintext,
    show once)."""
    user.totp_secret = secret
    user.totp_enabled_at = datetime.now(UTC)
    plain, hashed = generate_recovery_codes()
    user.totp_recovery_codes = hashed
    db.session.commit()
    return plain


def disable_totp(user: User) -> None:
    user.totp_secret = None
    user.totp_enabled_at = None
    user.totp_recovery_codes = None
    db.session.commit()


def regenerate_recovery_codes(user: User) -> list[str]:
    """Replace the user's recovery codes with a fresh set. Plaintext returned
    once for display."""
    plain, hashed = generate_recovery_codes()
    user.totp_recovery_codes = hashed
    db.session.commit()
    return plain
