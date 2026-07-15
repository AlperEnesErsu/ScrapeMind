"""API v1 write endpoints — flags, notes, idempotency, ownership."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape.models import Paper, PaperNote, UserPaper

PASSWORD = "secret12345"


@pytest.fixture
def ctx(db):
    """Two users, each with one linked paper — so ownership can be tested."""
    for tbl in (
        "paper_chat_messages",
        "paper_notes",
        "user_papers",
        "papers",
        "revoked_tokens",
        "audit_logs",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter(User.username.in_(["writer", "stranger"])).delete(
        synchronize_session=False
    )
    db.session.commit()

    users = {}
    for name in ("writer", "stranger"):
        u = User(
            username=name,
            email=f"{name}@example.test",
            full_name=name.title(),
            password_hash=LocalAuthStrategy.hash_password(PASSWORD),
            is_active=True,
        )
        db.session.add(u)
        users[name] = u
    db.session.commit()

    paper = Paper(
        source="arxiv",
        external_id="2401.99999",
        title="A Paper",
        abstract="abs",
        authors=["A. One"],
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        categories=["cs.LG"],
    )
    db.session.add(paper)
    db.session.commit()

    links = {}
    for name, u in users.items():
        link = UserPaper(user_id=u.id, paper_id=paper.id, matched_keyword="kw")
        db.session.add(link)
        links[name] = link
    db.session.commit()

    data = {
        "writer_id": users["writer"].id,
        "stranger_id": users["stranger"].id,
        "link_id": links["writer"].id,
        "stranger_link_id": links["stranger"].id,
    }
    yield data

    db.session.rollback()
    for tbl in (
        "paper_chat_messages",
        "paper_notes",
        "user_papers",
        "papers",
        "revoked_tokens",
        "audit_logs",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter(User.username.in_(["writer", "stranger"])).delete(
        synchronize_session=False
    )
    db.session.commit()


def _auth(client, username="writer"):
    r = client.post("/api/v1/auth/token", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.get_json()['access_token']}"}


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


def test_favorite_put_and_delete(client, ctx):
    h = _auth(client)
    lid = ctx["link_id"]

    r = client.put(f"/api/v1/me/papers/{lid}/favorite", headers=h)
    assert r.status_code == 200
    assert r.get_json()["data"]["is_favorite"] is True

    r = client.delete(f"/api/v1/me/papers/{lid}/favorite", headers=h)
    assert r.get_json()["data"]["is_favorite"] is False


def test_favorite_put_is_idempotent(client, ctx):
    """A retried PUT must not flip the flag back — that's why these aren't toggles."""
    h = _auth(client)
    lid = ctx["link_id"]
    for _ in range(3):
        r = client.put(f"/api/v1/me/papers/{lid}/favorite", headers=h)
        assert r.get_json()["data"]["is_favorite"] is True
    for _ in range(2):
        r = client.delete(f"/api/v1/me/papers/{lid}/favorite", headers=h)
        assert r.get_json()["data"]["is_favorite"] is False


def test_read_later_put_and_delete(client, ctx):
    h = _auth(client)
    lid = ctx["link_id"]
    assert (
        client.put(f"/api/v1/me/papers/{lid}/read-later", headers=h).get_json()["data"][
            "read_later"
        ]
        is True
    )
    assert (
        client.delete(f"/api/v1/me/papers/{lid}/read-later", headers=h).get_json()["data"][
            "read_later"
        ]
        is False
    )


def test_dismiss_and_undismiss(client, ctx):
    h = _auth(client)
    lid = ctx["link_id"]

    r = client.put(f"/api/v1/me/papers/{lid}/dismissed", headers=h)
    assert r.get_json()["data"]["dismissed_at"] is not None
    # Dismissed papers drop out of the feed listing.
    assert client.get("/api/v1/me/papers", headers=h).get_json()["pagination"]["total"] == 0

    r = client.delete(f"/api/v1/me/papers/{lid}/dismissed", headers=h)
    assert r.get_json()["data"]["dismissed_at"] is None
    assert client.get("/api/v1/me/papers", headers=h).get_json()["pagination"]["total"] == 1


def test_mark_seen_is_stable(client, ctx):
    h = _auth(client)
    lid = ctx["link_id"]
    first = client.post(f"/api/v1/me/papers/{lid}/seen", headers=h).get_json()["data"]["seen_at"]
    assert first is not None
    second = client.post(f"/api/v1/me/papers/{lid}/seen", headers=h).get_json()["data"]["seen_at"]
    assert second == first  # only stamped once


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def test_note_create_list_update_delete(client, ctx, db):
    h = _auth(client)
    lid = ctx["link_id"]

    r = client.post(
        f"/api/v1/me/papers/{lid}/notes", headers=h, json={"body": "İlk not", "tag": "soru"}
    )
    assert r.status_code == 201
    note = r.get_json()["data"]
    assert note["body"] == "İlk not"
    assert note["tag"] == "soru"

    listed = client.get(f"/api/v1/me/papers/{lid}/notes", headers=h).get_json()["data"]
    assert [n["id"] for n in listed] == [note["id"]]

    r = client.patch(f"/api/v1/notes/{note['id']}", headers=h, json={"body": "Düzeltilmiş"})
    assert r.status_code == 200
    assert r.get_json()["data"]["body"] == "Düzeltilmiş"
    assert r.get_json()["data"]["tag"] == "soru"  # omitted field kept

    r = client.delete(f"/api/v1/notes/{note['id']}", headers=h)
    assert r.status_code == 200
    assert r.get_json() == {"deleted": True}
    assert PaperNote.query.filter_by(id=note["id"]).first() is None


def test_note_empty_body_is_422(client, ctx):
    h = _auth(client)
    r = client.post(f"/api/v1/me/papers/{ctx['link_id']}/notes", headers=h, json={"body": "   "})
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "empty_body"


def test_note_unknown_tag_is_dropped_not_rejected(client, ctx):
    h = _auth(client)
    r = client.post(
        f"/api/v1/me/papers/{ctx['link_id']}/notes",
        headers=h,
        json={"body": "x", "tag": "bogus-tag"},
    )
    assert r.status_code == 201
    assert r.get_json()["data"]["tag"] is None


def test_note_patch_empty_body_is_422(client, ctx):
    h = _auth(client)
    created = client.post(
        f"/api/v1/me/papers/{ctx['link_id']}/notes", headers=h, json={"body": "keep"}
    ).get_json()["data"]
    r = client.patch(f"/api/v1/notes/{created['id']}", headers=h, json={"body": ""})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Ownership + auth
# ---------------------------------------------------------------------------


def test_cannot_touch_another_users_paper(client, ctx):
    h = _auth(client, "writer")
    other = ctx["stranger_link_id"]
    # 404 (not 403) so the API never confirms the row exists.
    assert client.put(f"/api/v1/me/papers/{other}/favorite", headers=h).status_code == 404
    assert client.put(f"/api/v1/me/papers/{other}/dismissed", headers=h).status_code == 404
    assert client.get(f"/api/v1/me/papers/{other}/notes", headers=h).status_code == 404
    assert (
        client.post(f"/api/v1/me/papers/{other}/notes", headers=h, json={"body": "x"}).status_code
        == 404
    )


def test_cannot_touch_another_users_note(client, ctx):
    stranger_h = _auth(client, "stranger")
    note = client.post(
        f"/api/v1/me/papers/{ctx['stranger_link_id']}/notes",
        headers=stranger_h,
        json={"body": "private"},
    ).get_json()["data"]

    writer_h = _auth(client, "writer")
    assert (
        client.patch(
            f"/api/v1/notes/{note['id']}", headers=writer_h, json={"body": "hack"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/notes/{note['id']}", headers=writer_h).status_code == 404
    # Still intact.
    assert PaperNote.query.filter_by(id=note["id"]).first().body == "private"


def test_writes_require_a_token(client, ctx):
    lid = ctx["link_id"]
    assert client.put(f"/api/v1/me/papers/{lid}/favorite").status_code == 401
    assert client.post(f"/api/v1/me/papers/{lid}/notes", json={"body": "x"}).status_code == 401
    assert client.delete("/api/v1/notes/1").status_code == 401


def test_missing_paper_is_404(client, ctx):
    h = _auth(client)
    assert client.put("/api/v1/me/papers/99999999/favorite", headers=h).status_code == 404
