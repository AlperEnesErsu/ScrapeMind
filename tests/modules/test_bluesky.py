"""Faz 5.3 — Bluesky Social Feed (UserBluesky) tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape.models import Paper, UserBluesky
from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.service import (
    BLUESKY_CAP_MESSAGE,
    add_user_bluesky,
    count_user_bluesky,
    get_user_bluesky,
    ingest_user_bluesky,
    list_user_bluesky,
    remove_user_bluesky,
    toggle_user_bluesky,
)
from app.modules.scrape.sources import bluesky_source
from app.modules.scrape.sources.payload import PaperPayload


@pytest.fixture
def clean_user(db):
    for tbl in (
        "notifications",
        "user_digests",
        "paper_notes",
        "user_papers",
        "papers",
        "user_sources",
        "user_feeds",
        "user_channels",
        "user_pages",
        "user_bluesky",
        "scan_runs",
        "user_settings",
        "user_keywords",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="bskyuser").delete()
    db.session.commit()
    u = User(
        username="bskyuser",
        email="bskyuser@example.test",
        full_name="Bluesky User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    for tbl in (
        "notifications",
        "user_digests",
        "paper_notes",
        "user_papers",
        "papers",
        "user_sources",
        "user_feeds",
        "user_channels",
        "user_pages",
        "user_bluesky",
        "scan_runs",
        "user_settings",
        "user_keywords",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="bskyuser").delete()
    db.session.commit()


@pytest.fixture
def mock_bluesky_slot(monkeypatch):
    monkeypatch.setattr(bluesky_source, "bluesky_slot", lambda: True)


# ----------------------------------------------------------------------------
# 1. Adapter Unit Tests
# ----------------------------------------------------------------------------


def test_resolve_account_handles_varied_formats(mock_bluesky_slot, monkeypatch):
    canned_profile = {
        "did": "did:plc:ylecun123",
        "handle": "ylecun.bsky.social",
        "displayName": "Yann LeCun",
        "avatar": "https://cdn.bsky.app/avatar.jpg",
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = canned_profile
    monkeypatch.setattr(bluesky_source.requests, "get", lambda *a, **kw: mock_resp)

    inputs = [
        "ylecun.bsky.social",
        "@ylecun.bsky.social",
        "https://bsky.app/profile/ylecun.bsky.social",
        "https://bsky.app/profile/did:plc:ylecun123",
    ]
    for inp in inputs:
        did, handle, name, avatar = bluesky_source.resolve_account(inp)
        assert did == "did:plc:ylecun123"
        assert handle == "ylecun.bsky.social"
        assert name == "Yann LeCun"
        assert avatar == "https://cdn.bsky.app/avatar.jpg"


def test_resolve_account_rejects_empty_and_not_found(mock_bluesky_slot, monkeypatch):
    with pytest.raises(ValueError):
        bluesky_source.resolve_account("")

    mock_404 = MagicMock()
    mock_404.status_code = 404
    monkeypatch.setattr(bluesky_source.requests, "get", lambda *a, **kw: mock_404)
    with pytest.raises(ValueError, match="Could not find that Bluesky account"):
        bluesky_source.resolve_account("nobody.bsky.social")


def test_resolve_account_respects_rate_limit(monkeypatch):
    monkeypatch.setattr(bluesky_source, "bluesky_slot", lambda: False)
    with pytest.raises(SourceThrottledError):
        bluesky_source.resolve_account("ylecun.bsky.social")


def test_fetch_author_posts_parses_feed(mock_bluesky_slot, monkeypatch):
    now_str = datetime.now(UTC).isoformat()
    canned_feed = {
        "feed": [
            {
                "post": {
                    "uri": "at://did:plc:ylecun123/app.bsky.feed.post/3l123",
                    "author": {
                        "handle": "ylecun.bsky.social",
                        "displayName": "Yann LeCun",
                    },
                    "record": {
                        "text": "Exciting progress in deep learning and energy-based models! #AI #ML",
                        "createdAt": now_str,
                    },
                    "embed": {
                        "external": {
                            "title": "Energy Based Models Paper",
                            "description": "Read our latest preprint on arXiv.",
                        }
                    },
                }
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = canned_feed
    monkeypatch.setattr(bluesky_source.requests, "get", lambda *a, **kw: mock_resp)

    payloads = bluesky_source.fetch_author_posts("ylecun.bsky.social")
    assert len(payloads) == 1
    p = payloads[0]
    assert p.source == "bluesky"
    assert p.kind == "social"
    assert p.external_id == "at://did:plc:ylecun123/app.bsky.feed.post/3l123"
    assert "Exciting progress" in p.title
    assert "Energy Based Models Paper" in (p.abstract or "")
    assert p.url == "https://bsky.app/profile/ylecun.bsky.social/post/3l123"
    assert "ai" in p.categories
    assert "ml" in p.categories
    assert "social" in p.categories


def test_fetch_author_posts_respects_since_watermark(mock_bluesky_slot, monkeypatch):
    t_old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    t_new = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    canned_feed = {
        "feed": [
            {
                "post": {
                    "uri": "at://did:plc:user1/app.bsky.feed.post/new",
                    "author": {"handle": "user1"},
                    "record": {"text": "New post", "createdAt": t_new},
                }
            },
            {
                "post": {
                    "uri": "at://did:plc:user1/app.bsky.feed.post/old",
                    "author": {"handle": "user1"},
                    "record": {"text": "Old post", "createdAt": t_old},
                }
            },
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = canned_feed
    monkeypatch.setattr(bluesky_source.requests, "get", lambda *a, **kw: mock_resp)

    since = datetime.now(UTC) - timedelta(days=1)
    payloads = bluesky_source.fetch_author_posts("user1", since=since)
    assert len(payloads) == 1
    assert payloads[0].title == "New post"


# ----------------------------------------------------------------------------
# 2. Service CRUD & Cap Tests
# ----------------------------------------------------------------------------


def test_add_user_bluesky_crud_and_idempotency(clean_user, monkeypatch):
    user = clean_user

    monkeypatch.setattr(
        bluesky_source,
        "resolve_account",
        lambda inp: (
            "did:plc:foo123",
            "foo.bsky.social",
            "Dr. Foo",
            "https://cdn.bsky.app/avatar.jpg",
        ),
    )

    # 1. Add account
    acc, err = add_user_bluesky(user, "foo.bsky.social")
    assert err is None
    assert acc is not None
    assert acc.handle == "foo.bsky.social"
    assert acc.did == "did:plc:foo123"
    assert acc.active is True
    assert count_user_bluesky(user) == 1

    # 2. Idempotent re-add
    acc2, err2 = add_user_bluesky(user, "foo.bsky.social")
    assert err2 is None
    assert acc2.id == acc.id
    assert count_user_bluesky(user) == 1

    # 3. Toggle off and re-add resumes
    toggle_user_bluesky(user, acc.id)
    assert get_user_bluesky(user, acc.id).active is False
    acc3, _ = add_user_bluesky(user, "foo.bsky.social")
    assert acc3.active is True

    # 4. Remove
    assert remove_user_bluesky(user, acc.id) is True
    assert count_user_bluesky(user) == 0


def test_add_user_bluesky_enforces_cap(clean_user, monkeypatch):
    user = clean_user

    monkeypatch.setattr(
        bluesky_source,
        "resolve_account",
        lambda inp: (f"did:plc:{inp}", f"{inp}.bsky.social", inp, None),
    )

    # Add up to cap (20)
    for i in range(20):
        acc, err = add_user_bluesky(user, f"account{i}")
        assert err is None
        assert acc is not None

    assert count_user_bluesky(user) == 20

    # 21st fails with cap message
    acc, err = add_user_bluesky(user, "account21")
    assert acc is None
    assert err == BLUESKY_CAP_MESSAGE


def test_ingest_user_bluesky_advances_watermark(clean_user, monkeypatch, db):
    user = clean_user

    acc = UserBluesky(
        user_id=user.id,
        did="did:plc:test1234",
        handle="test.bsky.social",
        display_name="Test Researcher",
        active=True,
    )
    db.session.add(acc)
    db.session.commit()

    post_time = datetime.now(UTC)

    fake_payload = PaperPayload(
        source="bluesky",
        external_id="at://did:plc:test1234/app.bsky.feed.post/1",
        title="Breaking scientific discovery on Bluesky",
        abstract="Here are the full results of our experiment.",
        authors=["Test Researcher"],
        url="https://bsky.app/profile/test.bsky.social/post/1",
        pdf_url=None,
        published_at=post_time,
        categories=["bluesky", "social"],
        kind="social",
    )

    monkeypatch.setattr(bluesky_source, "fetch_author_posts", lambda *a, **kw: [fake_payload])

    summary, touched = ingest_user_bluesky(user)
    assert summary["hits"] == 1
    assert summary["new"] == 1
    assert len(touched) == 1

    paper = Paper.query.filter_by(external_id="at://did:plc:test1234/app.bsky.feed.post/1").first()
    assert paper is not None
    assert paper.kind == "social"

    # Watermark advanced
    reloaded_acc = get_user_bluesky(user, acc.id)
    assert reloaded_acc.last_post_at == post_time


# ----------------------------------------------------------------------------
# 3. HTTP Endpoints & UI Lifecycle
# ----------------------------------------------------------------------------


def test_bluesky_routes_lifecycle(auth_client, monkeypatch):
    client, uid = auth_client

    monkeypatch.setattr(
        bluesky_source,
        "resolve_account",
        lambda inp: ("did:plc:route1", "route.bsky.social", "Route User", "https://cdn.bsky.app/a.png"),
    )

    # Add via POST
    r = client.post(
        "/papers/profile/bluesky/add",
        data={"handle": "route.bsky.social", "surface": "tab"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200

    user = User.query.get(uid)
    accounts = list_user_bluesky(user)
    assert len(accounts) == 1
    acc_id = accounts[0].id
    assert accounts[0].handle == "route.bsky.social"

    # Toggle off
    r = client.post(
        f"/papers/profile/bluesky/{acc_id}/toggle",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert list_user_bluesky(user)[0].active is False

    # Filter chips
    r = client.get("/papers/profile/bluesky?filter=active")
    assert r.status_code == 200
    assert "route.bsky.social" not in r.get_data(as_text=True)

    r = client.get("/papers/profile/bluesky?filter=paused")
    assert r.status_code == 200
    assert "route.bsky.social" in r.get_data(as_text=True)

    # Remove
    r = client.post(
        f"/papers/profile/bluesky/{acc_id}/remove",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert len(list_user_bluesky(user)) == 0


def test_feed_tasks_link_for_user_calls_bluesky_ingest(clean_user, monkeypatch):
    from app.tasks.feed_tasks import link_for_user

    user = clean_user
    called = {}

    def fake_ingest_bluesky(u):
        called["bluesky"] = True
        return {"hits": 5, "new": 2}, []

    monkeypatch.setattr("app.modules.scrape.service.ingest_user_bluesky", fake_ingest_bluesky)
    monkeypatch.setattr("app.modules.scrape.service.ingest_user_feeds", lambda u: ({"hits": 0}, []))
    monkeypatch.setattr("app.modules.scrape.service.ingest_user_pages", lambda u: ({"hits": 0}, []))
    monkeypatch.setattr(
        "app.modules.scrape.service.link_relevant_feed_items",
        lambda u, **kw: {"scored": 0, "linked": 0},
    )

    res = link_for_user(user.id)
    assert called.get("bluesky") is True
    assert res == {"scored": 0, "linked": 0}
