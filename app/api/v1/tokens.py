"""JWT issue/verify + revocation for the v1 API.

HS256 via Authlib (`authlib.jose`) — already a project dependency, so no
PyJWT. Two token types share one signing key:

- access  — short-lived, sent as `Authorization: Bearer <token>` on every call
- refresh — long-lived, exchanged at /auth/refresh for a fresh token pair

Revocation is split by token type so the hot path stays cheap:

- **access**: carries a `ver` claim mirroring `User.token_version`. The guard
  already loads the user, so comparing `ver` costs no extra query. Bumping
  token_version (password change, sign-out-everywhere) kills every
  outstanding token at once; stragglers die on their own within 15 minutes.
- **refresh**: carries a unique `jti`, checked against the `revoked_tokens`
  denylist. That lookup only happens on /auth/refresh and /auth/logout —
  never on a normal API call. Refresh is rotated on every use, so a stolen
  token is usable at most until the legitimate client next refreshes.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from authlib.jose import JoseError, jwt
from flask import current_app

from app.core.models.revoked_token import RevokedToken
from app.extensions import db

ACCESS = "access"
REFRESH = "refresh"


def _secret() -> str:
    # Runtime fallback (not at config-class definition time) so TestingConfig's
    # own SECRET_KEY is honoured even when JWT_SECRET_KEY is left empty.
    return current_app.config.get("JWT_SECRET_KEY") or current_app.config["SECRET_KEY"]


def _encode(payload: dict[str, Any]) -> str:
    header = {"alg": current_app.config["JWT_ALGORITHM"], "typ": "JWT"}
    token = jwt.encode(header, payload, _secret())
    return token.decode("ascii") if isinstance(token, bytes) else token


def _base_claims(user_id: int, token_type: str, ttl: int) -> dict[str, Any]:
    now = int(time.time())
    return {
        "sub": str(user_id),
        "type": token_type,
        "iat": now,
        "exp": now + ttl,
        "iss": current_app.config["JWT_ISSUER"],
        "jti": uuid.uuid4().hex,
    }


def issue_access_token(user) -> tuple[str, int]:
    """Return (token, ttl_seconds). Takes the User so we can stamp `ver`."""
    ttl = int(current_app.config["JWT_ACCESS_TTL"])
    claims = _base_claims(user.id, ACCESS, ttl)
    claims["ver"] = user.token_version or 0
    return _encode(claims), ttl


def issue_refresh_token(user) -> str:
    ttl = int(current_app.config["JWT_REFRESH_TTL"])
    claims = _base_claims(user.id, REFRESH, ttl)
    claims["ver"] = user.token_version or 0
    return _encode(claims)


def decode_token(token: str, expected_type: str) -> dict[str, Any] | None:
    """Return validated claims, or None if invalid/expired/wrong-type/wrong-issuer.

    Signature/expiry/type only — revocation is the caller's job (see
    `is_revoked` for refresh, `ver` comparison for access), because the two
    checks need different data.
    """
    try:
        claims = jwt.decode(token, _secret())
        claims.validate()  # enforces exp / nbf / iat
    except (JoseError, ValueError, KeyError):
        return None
    if claims.get("type") != expected_type:
        return None
    if claims.get("iss") != current_app.config["JWT_ISSUER"]:
        return None
    return dict(claims)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def is_revoked(jti: str | None) -> bool:
    """True if this refresh token's jti sits on the denylist."""
    if not jti:
        # A token minted before jti existed can't be revoked individually;
        # treat it as live and let `ver`/expiry handle it.
        return False
    return db.session.query(RevokedToken.query.filter_by(jti=jti).exists()).scalar()


def revoke_refresh_token(claims: dict[str, Any], user_id: int) -> bool:
    """Denylist a refresh token by its claims. Idempotent — re-revoking an
    already-listed jti is a no-op. Returns False when the token carries no
    jti (nothing to key on)."""
    jti = claims.get("jti")
    if not jti:
        return False
    if is_revoked(jti):
        return True
    exp = claims.get("exp")
    expires_at = datetime.fromtimestamp(int(exp), tz=UTC) if exp else datetime.now(UTC)
    db.session.add(RevokedToken(jti=jti, user_id=user_id, expires_at=expires_at))
    db.session.commit()
    return True


def revoke_all_for_user(user) -> None:
    """Invalidate every outstanding token for this user. Access tokens fail
    their `ver` check immediately; refresh tokens fail theirs at the next
    /auth/refresh. See `User.bump_token_version`."""
    user.bump_token_version()
    db.session.commit()
