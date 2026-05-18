"""Academic identity service layer.

Phase 2.0 surface: multi-email support with token-based verification.
The lookup table is generic so future identifier types (ORCID, Scopus, WoS)
slot in without touching this file's structure.
"""

from datetime import UTC, datetime

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.models.user import User
from app.extensions import db
from app.modules.academic.models import IdentifierType, UserIdentifier

EMAIL_VERIFY_SALT = "scrapemind-email-verify"
EMAIL_VERIFY_MAX_AGE = 24 * 3600  # 24h — gentler than password reset (1h)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=EMAIL_VERIFY_SALT)


def get_email_type() -> IdentifierType:
    """Return the canonical 'email' IdentifierType row (seeded at install)."""
    t = IdentifierType.query.filter_by(code="email").first()
    if t is None:
        raise RuntimeError("identifier_types seed missing: run scripts/seed.py")
    return t


def list_user_emails(user: User) -> list[UserIdentifier]:
    et = get_email_type()
    return (
        UserIdentifier.query.filter_by(user_id=user.id, identifier_type_id=et.id)
        .order_by(UserIdentifier.is_primary.desc(), UserIdentifier.created_at)
        .all()
    )


def add_email(
    user: User, value: str, *, verified: bool = False
) -> tuple[UserIdentifier | None, str | None]:
    """Add an email address to the user. Caller handles verification flow."""
    value = value.strip().lower()
    et = get_email_type()
    if UserIdentifier.query.filter_by(identifier_type_id=et.id, value=value).first():
        return None, "Email already in use."
    # Also defend against a value already living in users.email but not yet
    # mirrored into user_identifiers (legacy rows, partial migrations, tests).
    if User.query.filter(User.email == value, User.id != user.id).first():
        return None, "Email already in use."
    ident = UserIdentifier(
        user_id=user.id,
        identifier_type_id=et.id,
        value=value,
        is_primary=False,
        is_verified=verified,
        verified_at=datetime.now(UTC) if verified else None,
    )
    db.session.add(ident)
    db.session.commit()
    return ident, None


def make_email_verify_token(ident: UserIdentifier) -> str:
    return _serializer().dumps({"iid": ident.id, "v": ident.value})


def verify_email_token(token: str) -> UserIdentifier | None:
    try:
        data = _serializer().loads(token, max_age=EMAIL_VERIFY_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    ident = db.session.get(UserIdentifier, data.get("iid"))
    if ident is None or ident.is_deleted:
        return None
    # If the address has been rotated since the token was issued, refuse.
    if ident.value != data.get("v"):
        return None
    if not ident.is_verified:
        ident.is_verified = True
        ident.verified_at = datetime.now(UTC)
        db.session.commit()
    return ident


def set_primary_email(user: User, ident_id: int) -> tuple[bool, str | None]:
    """Make this email the primary one. Also syncs User.email."""
    ident = db.session.get(UserIdentifier, ident_id)
    if ident is None or ident.user_id != user.id or ident.is_deleted:
        return False, "Email not found."
    if not ident.is_verified:
        return False, "Verify the email before making it primary."
    et = get_email_type()
    # Demote previous primaries.
    UserIdentifier.query.filter(
        UserIdentifier.user_id == user.id,
        UserIdentifier.identifier_type_id == et.id,
        UserIdentifier.id != ident.id,
    ).update({"is_primary": False})
    ident.is_primary = True
    # Sync the canonical User.email column.
    user.email = ident.value
    db.session.commit()
    return True, None


def delete_email(user: User, ident_id: int) -> tuple[bool, str | None]:
    """Hard-delete an email row. The primary one cannot be removed directly."""
    ident = db.session.get(UserIdentifier, ident_id)
    if ident is None or ident.user_id != user.id or ident.is_deleted:
        return False, "Email not found."
    if ident.is_primary:
        return False, "Cannot delete the primary email. Promote another first."
    db.session.delete(ident)
    db.session.commit()
    return True, None


def ensure_primary_email_row(user: User) -> UserIdentifier:
    """Idempotently make sure the user has a primary email row that mirrors User.email.

    Used by the migration and by seed; also a self-healing call site if someone
    manages to delete the primary row out-of-band.
    """
    et = get_email_type()
    existing = UserIdentifier.query.filter_by(
        user_id=user.id, identifier_type_id=et.id, value=user.email
    ).first()
    if existing is None:
        existing = UserIdentifier(
            user_id=user.id,
            identifier_type_id=et.id,
            value=user.email,
            is_primary=True,
            is_verified=True,
            verified_at=datetime.now(UTC),
        )
        db.session.add(existing)
    else:
        existing.is_primary = True
        if not existing.is_verified:
            existing.is_verified = True
            existing.verified_at = datetime.now(UTC)
    db.session.commit()
    return existing
