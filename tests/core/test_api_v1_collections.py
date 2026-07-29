"""Tests for API v1 collections and feeds endpoints."""

from __future__ import annotations


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
