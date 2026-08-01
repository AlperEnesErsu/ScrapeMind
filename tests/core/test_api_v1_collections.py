"""Tests for API v1 collections and feeds endpoints."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User

PASSWORD = "secret12345"


@pytest.fixture
def token(client, db):
    """A user + a valid API access token. Yields the token string.

    Teardown clears the FK children the collection/feed endpoints create
    before deleting the user, so a follow-up test's user delete can't hit a
    foreign-key violation.
    """
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).filter_by(username="apicoll").delete()
    db.session.commit()
    u = User(
        username="apicoll",
        email="apicoll@example.test",
        full_name="API Coll",
        password_hash=LocalAuthStrategy.hash_password(PASSWORD),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    uid = u.id

    resp = client.post("/api/v1/auth/token", json={"username": "apicoll", "password": PASSWORD})
    assert resp.status_code == 200
    yield resp.get_json()["access_token"]

    db.session.rollback()
    # collection_papers has no user_id — deleting collections cascades to it
    # (FK ON DELETE CASCADE), so it's not in this per-user loop.
    for tbl in ("collections", "user_feeds", "user_authors", "revoked_tokens", "audit_logs"):
        db.session.execute(text(f"DELETE FROM {tbl} WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


def test_api_v1_list_and_create_collections(client, token):
    # GET empty list
    resp = client.get("/api/v1/me/collections", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json["data"] == []

    # POST create collection
    resp = client.post(
        "/api/v1/me/collections",
        json={"name": "API Test Coll", "description": "Desc"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert resp.json["data"]["name"] == "API Test Coll"

    # GET list again
    resp = client.get("/api/v1/me/collections", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert len(resp.json["data"]) == 1


def test_api_v1_list_feeds(client, token):
    resp = client.get("/api/v1/me/feeds", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert isinstance(resp.json["data"], list)
