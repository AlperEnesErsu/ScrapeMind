"""Bearer-token auth guard for v1 API views."""

from __future__ import annotations

from functools import wraps

from flask import g, request

from app.api.v1.errors import error_response
from app.api.v1.tokens import ACCESS, decode_token
from app.core.models.user import User


def _bearer_token() -> str | None:
    header = request.headers.get("Authorization", "")
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def jwt_required(f):
    """Require a valid access token. Sets `g.api_user` to the authenticated user."""

    @wraps(f)
    def wrapped(*args, **kwargs):
        token = _bearer_token()
        if not token:
            return error_response(401, "authorization_required", "Bearer token missing.")
        claims = decode_token(token, ACCESS)
        if claims is None:
            return error_response(401, "invalid_token", "Token is invalid or expired.")
        try:
            uid = int(claims["sub"])
        except (KeyError, TypeError, ValueError):
            return error_response(401, "invalid_token", "Token subject is malformed.")
        user = User.query.filter_by(id=uid, deleted_at=None).first()
        if user is None or not user.is_active:
            return error_response(401, "user_inactive", "User is inactive or no longer exists.")
        # Bulk revocation: a bumped token_version retires every token issued
        # before it. Free — the user row is already loaded.
        if claims.get("ver", 0) != (user.token_version or 0):
            return error_response(401, "token_revoked", "Token has been revoked.")
        g.api_user = user
        return f(*args, **kwargs)

    return wrapped
