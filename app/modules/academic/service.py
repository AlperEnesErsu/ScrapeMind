"""Academic identity service layer.

Two areas:
  * Identifiers — historical/alternate academic identities (NOT the
    login email). Multi-allowed, optionally verified.
  * Keywords  — research interest tags (global lookup + user junction).
"""

import re
from datetime import UTC, datetime

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.models.user import User
from app.extensions import db
from app.modules.academic.models import IdentifierType, Keyword, UserIdentifier, UserKeyword

EMAIL_VERIFY_SALT = "scrapemind-email-verify"
EMAIL_VERIFY_MAX_AGE = 24 * 3600  # 24h


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=EMAIL_VERIFY_SALT)


# --------------------------------------------------------------------------
# Identifier types
# --------------------------------------------------------------------------


def get_identifier_type(code: str) -> IdentifierType:
    t = IdentifierType.query.filter_by(code=code).first()
    if t is None:
        raise RuntimeError(f"identifier_types seed missing: '{code}' — run scripts/seed.py")
    return t


def get_email_type() -> IdentifierType:
    return get_identifier_type("email")


# --------------------------------------------------------------------------
# Identifiers (any kind — email, ORCID, Scopus, …)
# --------------------------------------------------------------------------


def list_user_identifiers(user: User, *, type_code: str | None = None) -> list[UserIdentifier]:
    q = UserIdentifier.query.filter_by(user_id=user.id)
    if type_code is not None:
        q = q.join(IdentifierType).filter(IdentifierType.code == type_code)
    return q.order_by(UserIdentifier.created_at).all()


def add_identifier(
    user: User, type_code: str, value: str, *, verified: bool = False
) -> tuple[UserIdentifier | None, str | None]:
    """Add an academic identifier. Refuses if it duplicates User.email or
    is already owned (by anyone). Returns (identifier, error_msgid)."""
    value = value.strip()
    if not value:
        return None, "Value cannot be empty."

    t = get_identifier_type(type_code)

    if type_code == "email":
        value = value.lower()
        # Block the auth email — it belongs to the User row, not here.
        if value == (user.email or "").lower():
            return None, "This email is already your login email — no need to add it here."

    # Regex (if any)
    if t.validation_regex:
        if not re.match(t.validation_regex, value):
            return None, "Value does not match the expected format."

    # Already owned (by anyone) for this type → reject
    if UserIdentifier.query.filter_by(identifier_type_id=t.id, value=value).first():
        return None, "This identifier is already registered."

    # Also catch the "value matches some other user's User.email" case for the email type
    if type_code == "email" and User.query.filter(User.email == value, User.id != user.id).first():
        return None, "This identifier is already registered."

    ident = UserIdentifier(
        user_id=user.id,
        identifier_type_id=t.id,
        value=value,
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
    if ident.value != data.get("v"):
        return None
    if not ident.is_verified:
        ident.is_verified = True
        ident.verified_at = datetime.now(UTC)
        db.session.commit()
    return ident


def delete_identifier(user: User, ident_id: int) -> tuple[bool, str | None]:
    ident = db.session.get(UserIdentifier, ident_id)
    if ident is None or ident.user_id != user.id or ident.is_deleted:
        return False, "Identifier not found."
    db.session.delete(ident)
    db.session.commit()
    return True, None


# --------------------------------------------------------------------------
# Keywords
# --------------------------------------------------------------------------


def _normalise_keyword(raw: str) -> str:
    return " ".join(raw.lower().split())[:64].strip()


def list_user_keywords(user: User) -> list[Keyword]:
    rows = UserKeyword.query.filter_by(user_id=user.id).join(Keyword).order_by(Keyword.value).all()
    return [r.keyword for r in rows]


def add_user_keyword(user: User, raw_value: str) -> tuple[Keyword | None, str | None]:
    value = _normalise_keyword(raw_value)
    if not value or len(value) < 2:
        return None, "Keyword too short."

    keyword = Keyword.query.filter_by(value=value).first()
    if keyword is None:
        keyword = Keyword(value=value)
        db.session.add(keyword)
        db.session.flush()

    if UserKeyword.query.filter_by(user_id=user.id, keyword_id=keyword.id).first():
        return None, "You already follow this keyword."

    db.session.add(UserKeyword(user_id=user.id, keyword_id=keyword.id))
    db.session.commit()
    return keyword, None


def remove_user_keyword(user: User, keyword_id: int) -> tuple[bool, str | None]:
    link = UserKeyword.query.filter_by(user_id=user.id, keyword_id=keyword_id).first()
    if link is None:
        return False, "Keyword not found."
    db.session.delete(link)
    db.session.commit()
    return True, None
