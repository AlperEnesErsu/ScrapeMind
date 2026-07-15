"""API v1 token revocation — logout, refresh rotation, bulk version bump."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.revoked_token import RevokedToken
from app.core.models.user import User

PASSWORD = "secret12345"


@pytest.fixture
def api_user(db):
    db.session.execute(text("DELETE FROM revoked_tokens"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).filter_by(username="revoker").delete()
    db.session.commit()
    u = User(
        username="revoker",
        email="revoker@example.test",
        full_name="Revoker",
        password_hash=LocalAuthStrategy.hash_password(PASSWORD),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    uid = u.id
    yield u
    db.session.rollback()
    db.session.execute(text("DELETE FROM revoked_tokens WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


def _tokens(client):
    r = client.post("/api/v1/auth/token", json={"username": "revoker", "password": PASSWORD})
    assert r.status_code == 200
    body = r.get_json()
    return body["access_token"], body["refresh_token"]


def _me(client, access):
    return client.get("/api/v1/me", headers={"Authorization": f"Bearer {access}"})


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_revokes_refresh_token(client, api_user, db):
    _, refresh = _tokens(client)

    r = client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert r.status_code == 200
    assert r.get_json() == {"revoked": True}
    assert RevokedToken.query.filter_by(user_id=api_user.id).count() == 1

    # The burned token can no longer buy a new access token.
    again = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert again.status_code == 401
    assert again.get_json()["error"]["code"] == "token_revoked"


def test_logout_is_idempotent(client, api_user, db):
    _, refresh = _tokens(client)
    assert client.post("/api/v1/auth/logout", json={"refresh_token": refresh}).status_code == 200
    # Second logout still reports success and doesn't duplicate the row.
    assert client.post("/api/v1/auth/logout", json={"refresh_token": refresh}).status_code == 200
    assert RevokedToken.query.filter_by(user_id=api_user.id).count() == 1


def test_logout_requires_token(client, api_user):
    r = client.post("/api/v1/auth/logout", json={})
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "missing_token"


def test_logout_accepts_garbage_without_leaking_state(client, api_user):
    # Don't tell a caller whether an unparseable token was ever real.
    r = client.post("/api/v1/auth/logout", json={"refresh_token": "not.a.jwt"})
    assert r.status_code == 200
    assert r.get_json() == {"revoked": True}


# ---------------------------------------------------------------------------
# Refresh rotation
# ---------------------------------------------------------------------------


def test_refresh_rotates_and_burns_the_old_token(client, api_user, db):
    _, refresh = _tokens(client)

    r = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200
    body = r.get_json()
    new_refresh = body["refresh_token"]
    assert new_refresh and new_refresh != refresh

    # Old one is dead...
    reused = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert reused.status_code == 401
    assert reused.get_json()["error"]["code"] == "token_revoked"

    # ...and the new one works.
    ok = client.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert ok.status_code == 200


def test_rotated_access_token_still_authenticates(client, api_user):
    _, refresh = _tokens(client)
    body = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh}).get_json()
    assert _me(client, body["access_token"]).status_code == 200


# ---------------------------------------------------------------------------
# Bulk revocation via token_version
# ---------------------------------------------------------------------------


def test_version_bump_kills_existing_access_token(client, api_user, db):
    access, _ = _tokens(client)
    assert _me(client, access).status_code == 200

    api_user.bump_token_version()
    db.session.commit()

    r = _me(client, access)
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "token_revoked"


def test_version_bump_kills_existing_refresh_token(client, api_user, db):
    _, refresh = _tokens(client)
    api_user.bump_token_version()
    db.session.commit()

    r = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "token_revoked"


def test_password_change_revokes_api_tokens(client, api_user, db):
    from app.core.settings.service import change_password

    access, refresh = _tokens(client)
    assert _me(client, access).status_code == 200

    ok, err = change_password(api_user, PASSWORD, "brandnewpass99")
    assert ok is True and err is None

    assert _me(client, access).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": refresh}).status_code == 401


def test_password_reset_revokes_api_tokens(client, api_user, db):
    from app.core.auth.service import reset_password

    access, _ = _tokens(client)
    assert _me(client, access).status_code == 200

    reset_password(api_user, "resetpass12345")
    assert _me(client, access).status_code == 401


def test_fresh_tokens_work_after_bump(client, api_user, db):
    """A bump retires old tokens but must not break re-authentication."""
    api_user.bump_token_version()
    db.session.commit()
    access, _ = _tokens(client)
    assert _me(client, access).status_code == 200


# ---------------------------------------------------------------------------
# Denylist purge
# ---------------------------------------------------------------------------


def test_purge_drops_only_expired_denylist_rows(db, api_user):
    from app.tasks.core_tasks import purge_revoked_tokens

    now = datetime.now(UTC)
    db.session.add_all(
        [
            RevokedToken(jti="dead1", user_id=api_user.id, expires_at=now - timedelta(days=1)),
            RevokedToken(jti="dead2", user_id=api_user.id, expires_at=now - timedelta(hours=1)),
            RevokedToken(jti="live1", user_id=api_user.id, expires_at=now + timedelta(days=5)),
        ]
    )
    db.session.commit()

    assert purge_revoked_tokens.delay().get() == {"deleted": 2}
    remaining = [r.jti for r in RevokedToken.query.filter_by(user_id=api_user.id).all()]
    assert remaining == ["live1"]
