"""JWT issue/verify for the v1 API.

HS256 via Authlib (`authlib.jose`) — already a project dependency, so no
PyJWT. Two token types share one signing key:

- access  — short-lived, sent as `Authorization: Bearer <token>` on every call
- refresh — long-lived, exchanged at /auth/refresh for a fresh access token

There is no server-side revocation list in v1: a refresh token is valid until
it expires. Deactivating/deleting the user is still honoured because every
request re-loads the user and rejects inactive/deleted accounts.
"""

from __future__ import annotations

import time
from typing import Any

from authlib.jose import JoseError, jwt
from flask import current_app

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
    }


def issue_access_token(user_id: int) -> tuple[str, int]:
    """Return (token, ttl_seconds)."""
    ttl = int(current_app.config["JWT_ACCESS_TTL"])
    return _encode(_base_claims(user_id, ACCESS, ttl)), ttl


def issue_refresh_token(user_id: int) -> str:
    ttl = int(current_app.config["JWT_REFRESH_TTL"])
    return _encode(_base_claims(user_id, REFRESH, ttl))


def decode_token(token: str, expected_type: str) -> dict[str, Any] | None:
    """Return validated claims, or None if invalid/expired/wrong-type/wrong-issuer."""
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
