"""API v1 JWTs after the move from `authlib.jose` to `joserfc` (PRELAUNCH O10).

The load-bearing property is continuity: refresh tokens live for days, so at
deploy time there are outstanding tokens signed by the old implementation.
If the new verifier refused them, every API client would be signed out at
once. The rest pins what the switch tightened -- the configured algorithm is
now enforced -- and what it must keep refusing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import warnings

import pytest

from app.api.v1 import tokens


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _claims(app, **overrides) -> dict:
    now = int(time.time())
    base = {
        "sub": "1",
        "type": tokens.REFRESH,
        "iat": now,
        "exp": now + 600,
        "iss": app.config["JWT_ISSUER"],
        "jti": "abc",
        "ver": 0,
    }
    base.update(overrides)
    return base


def _hmac_token(secret: str, alg: str, digest, claims: dict) -> str:
    signing_input = _b64({"alg": alg, "typ": "JWT"}) + "." + _b64(claims)
    sig = hmac.new(secret.encode(), signing_input.encode(), digest).digest()
    return signing_input + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def test_a_token_signed_by_the_old_implementation_still_verifies(app):
    """Outstanding refresh tokens from before the deploy must keep working."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from authlib.jose import jwt as authlib_jwt

    with app.app_context():
        claims = _claims(app)
        old = authlib_jwt.encode({"alg": "HS256", "typ": "JWT"}, claims, tokens._secret())
        old = old.decode("ascii") if isinstance(old, bytes) else old
        assert tokens.decode_token(old, tokens.REFRESH) == claims


def test_round_trip(app):
    with app.app_context():
        claims = _claims(app, type=tokens.ACCESS)
        assert tokens.decode_token(tokens._encode(claims), tokens.ACCESS) == claims


def test_a_different_hmac_algorithm_is_refused(app):
    """`authlib.jose` accepted this under an HS256 config; the setting is now
    enforced. Signed with the real secret, so only the algorithm is wrong."""
    with app.app_context():
        assert app.config["JWT_ALGORITHM"] == "HS256"
        forged = _hmac_token(tokens._secret(), "HS512", hashlib.sha512, _claims(app))
        assert tokens.decode_token(forged, tokens.REFRESH) is None


def test_alg_none_is_refused(app):
    with app.app_context():
        unsigned = _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(_claims(app)) + "."
        assert tokens.decode_token(unsigned, tokens.REFRESH) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 5},  # expired
        {"nbf": int(time.time()) + 3600},  # not yet valid
    ],
)
def test_time_claims_are_enforced_without_leeway(app, overrides):
    with app.app_context():
        assert (
            tokens.decode_token(tokens._encode(_claims(app, **overrides)), tokens.REFRESH) is None
        )


def test_a_wrong_secret_is_refused(app):
    with app.app_context():
        forged = _hmac_token("not-the-secret", "HS256", hashlib.sha256, _claims(app))
        assert tokens.decode_token(forged, tokens.REFRESH) is None


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "x" * 5000])
def test_garbage_is_refused_not_raised(app, garbage):
    with app.app_context():
        assert tokens.decode_token(garbage, tokens.REFRESH) is None


def test_the_deprecated_module_is_no_longer_what_verifies():
    """Checked on the imported object, not the source text -- the module's
    comments legitimately mention `authlib.jose` to explain the switch."""
    assert tokens.jwt.__name__.startswith("joserfc")
    assert tokens.JoseError.__module__.startswith("joserfc")
