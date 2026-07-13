"""API v1 — JWT auth flow + protected resource endpoints."""

from datetime import UTC, datetime

import pyotp
import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User

PASSWORD = "secret12345"


@pytest.fixture
def api_user(db):
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).filter_by(username="apitester").delete()
    db.session.commit()
    u = User(
        username="apitester",
        email="apitester@example.test",
        full_name="API Tester",
        password_hash=LocalAuthStrategy.hash_password(PASSWORD),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    uid = u.id
    yield u
    db.session.rollback()
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


def _get_token(client, username=None, password=PASSWORD, **extra):
    body = {"username": username or "apitester", "password": password, **extra}
    return client.post("/api/v1/auth/token", json=body)


# --------------------------------------------------------------------------
# Unauthenticated / auth failures
# --------------------------------------------------------------------------


def test_health_needs_no_auth(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.get_json() == {"status": "ok"}


def test_me_without_token_is_401(client):
    r = client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "authorization_required"


def test_token_missing_fields_is_422(client, api_user):
    r = client.post("/api/v1/auth/token", json={"username": "apitester"})
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "missing_credentials"


def test_token_bad_password_is_401(client, api_user):
    r = _get_token(client, password="wrongpass")
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "invalid_credentials"


def test_me_with_garbage_token_is_401(client):
    r = client.get("/api/v1/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "invalid_token"


def test_unknown_api_path_returns_json_404(client):
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_token_and_me_roundtrip(client, api_user):
    r = _get_token(client)
    assert r.status_code == 200
    body = r.get_json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["expires_in"] > 0

    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    data = me.get_json()["data"]
    assert data["username"] == "apitester"
    # Sensitive columns must never be serialized.
    assert "password_hash" not in data
    assert "totp_secret" not in data


def test_papers_list_is_paginated(client, api_user):
    tok = _get_token(client).get_json()["access_token"]
    r = client.get("/api/v1/papers?per_page=5", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    body = r.get_json()
    assert "data" in body and isinstance(body["data"], list)
    assert body["pagination"]["per_page"] == 5
    assert set(body["pagination"]) == {"page", "per_page", "total", "pages"}


def test_papers_per_page_capped(client, api_user):
    tok = _get_token(client).get_json()["access_token"]
    r = client.get("/api/v1/papers?per_page=9999", headers={"Authorization": f"Bearer {tok}"})
    assert r.get_json()["pagination"]["per_page"] == 100


def test_missing_paper_is_404(client, api_user):
    tok = _get_token(client).get_json()["access_token"]
    r = client.get("/api/v1/papers/99999999", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "not_found"


def test_my_papers_ok(client, api_user):
    tok = _get_token(client).get_json()["access_token"]
    r = client.get("/api/v1/me/papers", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "data" in r.get_json()


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------


def test_refresh_issues_working_access_token(client, api_user):
    refresh = _get_token(client).get_json()["refresh_token"]
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200
    new_access = r.get_json()["access_token"]
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200


def test_refresh_rejects_access_token(client, api_user):
    # An access token must not be usable at the refresh endpoint (wrong type).
    access = _get_token(client).get_json()["access_token"]
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": access})
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "invalid_token"


# --------------------------------------------------------------------------
# 2FA interplay
# --------------------------------------------------------------------------


def test_token_requires_otp_when_2fa_enabled(client, api_user, db):
    secret = pyotp.random_base32()
    api_user.totp_secret = secret
    api_user.totp_enabled_at = datetime.now(UTC)
    db.session.commit()

    # Without an OTP code → rejected.
    r = _get_token(client)
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "otp_required"

    # With a valid OTP code → tokens issued.
    code = pyotp.TOTP(secret).now()
    r2 = _get_token(client, otp_code=code)
    assert r2.status_code == 200
    assert r2.get_json()["access_token"]
