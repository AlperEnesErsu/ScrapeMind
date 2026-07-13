"""Token endpoints — password grant (+ 2FA) and refresh."""

from __future__ import annotations

from flask import request

from app.api.v1 import api_v1_bp
from app.api.v1.errors import error_response
from app.api.v1.tokens import (
    REFRESH,
    decode_token,
    issue_access_token,
    issue_refresh_token,
)
from app.core.audit.middleware import log_action
from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.auth.totp_service import consume_recovery_code, verify_totp
from app.core.models.user import User
from app.extensions import limiter


def _payload():
    """Accept either a JSON body or classic form encoding."""
    return request.get_json(silent=True) or request.form


@api_v1_bp.route("/auth/token", methods=["POST"])
@limiter.limit("10 per minute")
def issue_token():
    data = _payload()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    otp = (data.get("otp_code") or "").strip()

    if not username or not password:
        return error_response(422, "missing_credentials", "username and password are required.")

    # Reuse the web login strategy — same brute-force lockout + timing pad.
    user = LocalAuthStrategy().authenticate({"username": username, "password": password})
    if user is None:
        return error_response(401, "invalid_credentials", "Invalid username or password.")

    # Honour 2FA: an account with TOTP enabled must present a valid code
    # (authenticator OTP or a one-shot recovery code) to mint tokens.
    if user.is_totp_enabled:
        if not otp:
            return error_response(
                401, "otp_required", "This account requires a 2FA code (otp_code)."
            )
        if not (verify_totp(user.totp_secret, otp) or consume_recovery_code(user, otp)):
            return error_response(401, "invalid_otp", "Invalid 2FA code.")

    access, ttl = issue_access_token(user.id)
    refresh = issue_refresh_token(user.id)
    log_action("api.token_issued", entity_type="user", entity_id=user.id, user_id=user.id)
    return {
        "token_type": "Bearer",
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": ttl,
    }


@api_v1_bp.route("/auth/refresh", methods=["POST"])
@limiter.limit("30 per minute")
def refresh_token():
    data = _payload()
    token = (data.get("refresh_token") or "").strip()
    if not token:
        return error_response(422, "missing_token", "refresh_token is required.")

    claims = decode_token(token, REFRESH)
    if claims is None:
        return error_response(401, "invalid_token", "Refresh token is invalid or expired.")
    try:
        uid = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        return error_response(401, "invalid_token", "Refresh token subject is malformed.")

    user = User.query.filter_by(id=uid, deleted_at=None).first()
    if user is None or not user.is_active:
        return error_response(401, "user_inactive", "User is inactive or no longer exists.")

    access, ttl = issue_access_token(user.id)
    return {"token_type": "Bearer", "access_token": access, "expires_in": ttl}
