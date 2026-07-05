"""HTTP-level tests for the researcher-facing user panel.

Covers the routes added in the RAG-chat / notifications / read-later /
bulk-action / notes-export work. These drive Flask's test client through
real request/response cycles (auth_client fixture logs a user in), so they
exercise routes.py + service.py + dashboard/routes.py end to end.

Claude is never called for real: the chat test monkeypatches ask_paper +
is_ai_enabled so no network / API key is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.extensions import db as _db
from app.modules.scrape.models import Paper, PaperNote, UserPaper


def _make_paper(ext_id: str = "2401.90001", *, title: str = "Attention Is All You Need") -> Paper:
    paper = Paper(
        source="arxiv",
        external_id=ext_id,
        title=title,
        abstract="A transformer paper about attention.",
        authors=["A. Vaswani", "N. Shazeer"],
        url=f"http://arxiv.org/abs/{ext_id}",
        pdf_url=f"http://arxiv.org/pdf/{ext_id}",
        published_at=datetime(2026, 1, 15, tzinfo=UTC),
        categories=["cs.LG"],
    )
    _db.session.add(paper)
    _db.session.commit()
    return paper


def _link(uid: int, paper: Paper, **kw) -> UserPaper:
    link = UserPaper(user_id=uid, paper_id=paper.id, matched_keyword="transformer", **kw)
    _db.session.add(link)
    _db.session.commit()
    return link


@pytest.fixture
def clean_papers(db):
    """Wipe the paper/notes tables around each test so ids/counters reset."""
    for tbl in (
        "notifications",
        "paper_chat_messages",
        "paper_notes",
        "user_papers",
        "papers",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.commit()
    yield
    for tbl in (
        "notifications",
        "paper_chat_messages",
        "paper_notes",
        "user_papers",
        "papers",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.commit()


# --------------------------------------------------------------------------
# Auth gate — every user route rejects anonymous callers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/papers/"),
        ("get", "/library/"),
        ("get", "/notifications"),
        ("post", "/papers/1/read-later/toggle"),
        ("post", "/papers/1/favorite/toggle"),
        ("post", "/papers/bulk-action"),
        ("get", "/papers/1/export-notes"),
        ("post", "/papers/1/chat"),
    ],
)
def test_user_routes_require_login(client, method, path):
    r = getattr(client, method)(path, follow_redirects=False)
    assert r.status_code in (302, 401), f"{method} {path} → {r.status_code}"


# --------------------------------------------------------------------------
# Feed + detail render for a logged-in user
# --------------------------------------------------------------------------


def test_feed_renders_for_logged_in_user(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_paper()
    _link(uid, paper)
    r = client.get("/papers/")
    assert r.status_code == 200
    assert b"Attention Is All You Need" in r.data


def test_detail_marks_seen(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    assert link.seen_at is None
    r = client.get(f"/papers/{link.id}")
    assert r.status_code == 200
    _db.session.refresh(link)
    assert link.seen_at is not None


def test_detail_404_for_unknown_paper(auth_client, clean_papers):
    client, _uid = auth_client
    # get_user_paper filters on user_id, so a non-existent (or someone
    # else's) id both resolve to None → 404. Using a missing id exercises
    # the same guard without an FK-violating fixture row.
    r = client.get("/papers/999999")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Read-later toggle
# --------------------------------------------------------------------------


def test_read_later_toggle_htmx(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    r = client.post(
        f"/papers/{link.id}/read-later/toggle",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    _db.session.refresh(link)
    assert link.read_later is True
    # Toggle back
    client.post(f"/papers/{link.id}/read-later/toggle", headers={"HX-Request": "true"})
    _db.session.refresh(link)
    assert link.read_later is False


def test_favorite_toggle_button_variant(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    r = client.post(
        f"/papers/{link.id}/favorite/toggle?as=button",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    _db.session.refresh(link)
    assert link.is_favorite is True


# --------------------------------------------------------------------------
# Bulk action
# --------------------------------------------------------------------------


def test_bulk_action_favorite(auth_client, clean_papers):
    client, uid = auth_client
    p1, p2 = _make_paper("2401.1"), _make_paper("2401.2")
    l1, l2 = _link(uid, p1), _link(uid, p2)
    r = client.post(
        "/papers/bulk-action",
        data={"paper_ids": [str(l1.id), str(l2.id)], "action": "favorite"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    _db.session.refresh(l1)
    _db.session.refresh(l2)
    assert l1.is_favorite and l2.is_favorite


def test_bulk_action_dismiss_and_read_later(auth_client, clean_papers):
    client, uid = auth_client
    p = _make_paper("2401.3")
    link = _link(uid, p)
    client.post("/papers/bulk-action", data={"paper_ids": [str(link.id)], "action": "read_later"})
    _db.session.refresh(link)
    assert link.read_later is True

    client.post("/papers/bulk-action", data={"paper_ids": [str(link.id)], "action": "dismiss"})
    _db.session.refresh(link)
    assert link.dismissed_at is not None


def test_bulk_action_empty_selection_redirects(auth_client, clean_papers):
    client, _uid = auth_client
    r = client.post("/papers/bulk-action", data={"action": "favorite"}, follow_redirects=False)
    assert r.status_code in (302, 303)


# --------------------------------------------------------------------------
# Notes: add / edit / view / delete / export
# --------------------------------------------------------------------------


def test_note_add_edit_delete_cycle(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)

    # add
    client.post(
        f"/papers/{link.id}/notes",
        data={"body": "İlk not", "tag": "soru"},
        headers={"HX-Request": "true"},
    )
    note = PaperNote.query.filter_by(user_paper_id=link.id).first()
    assert note is not None and note.tag == "soru"

    # view partial
    assert client.get(f"/papers/notes/{note.id}/view").status_code == 200

    # edit
    client.post(
        f"/papers/notes/{note.id}/edit",
        data={"body": "Güncellenmiş not", "tag": "sonuç"},
        headers={"HX-Request": "true"},
    )
    _db.session.refresh(note)
    assert note.body == "Güncellenmiş not"
    assert note.tag == "sonuç"

    # delete
    client.post(f"/papers/notes/{note.id}/delete", headers={"HX-Request": "true"})
    assert PaperNote.query.filter_by(id=note.id).first() is None


def test_note_empty_body_rejected(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    client.post(
        f"/papers/{link.id}/notes",
        data={"body": "   ", "tag": ""},
        headers={"HX-Request": "true"},
    )
    assert PaperNote.query.filter_by(user_paper_id=link.id).count() == 0


def test_export_notes_markdown(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    _db.session.add(PaperNote(user_paper_id=link.id, body="Deney notu", tag="deney"))
    _db.session.commit()

    r = client.get(f"/papers/{link.id}/export-notes")
    assert r.status_code == 200
    assert r.mimetype == "text/markdown"
    assert b"Deney notu" in r.data
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_cite_bibtex(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    r = client.get(f"/papers/{link.id}/cite")
    assert r.status_code == 200
    assert b"@misc" in r.data or b"@article" in r.data
    assert b"Vaswani" in r.data


# --------------------------------------------------------------------------
# RAG chat — Claude mocked
# --------------------------------------------------------------------------


def test_chat_message_persists_and_answers(auth_client, clean_papers, monkeypatch):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)

    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda: True)
    monkeypatch.setattr(
        "app.modules.scrape.ai_service.ask_paper",
        lambda paper, question, history=None: "Bu bir test cevabıdır.",
    )

    r = client.post(f"/papers/{link.id}/chat", data={"message": "Bu makale ne anlatıyor?"})
    assert r.status_code == 200
    assert "Bu bir test cevabıdır." in r.get_data(as_text=True)

    from app.modules.scrape.models import PaperChatMessage

    msgs = PaperChatMessage.query.filter_by(user_paper_id=link.id).all()
    roles = [m.role for m in msgs]
    assert "user" in roles and "assistant" in roles


def test_chat_empty_message_no_content(auth_client, clean_papers, monkeypatch):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda: True)
    r = client.post(f"/papers/{link.id}/chat", data={"message": "   "})
    assert r.status_code == 204


def test_chat_blocked_when_ai_disabled(auth_client, clean_papers, monkeypatch):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda: False)
    r = client.post(f"/papers/{link.id}/chat", data={"message": "Merhaba"})
    assert r.status_code == 400


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------


def test_notifications_list_marks_read(auth_client, clean_papers):
    client, uid = auth_client
    from app.core.models.notification import Notification, add_notification

    add_notification(uid, "Test", "Bir bildirim")
    r = client.get("/notifications")
    assert r.status_code == 200
    # After viewing, all fetched notifications should be flagged read.
    unread = Notification.query.filter_by(user_id=uid, is_read=False).count()
    assert unread == 0


# --------------------------------------------------------------------------
# Dashboard — interest add / remove + index render
# --------------------------------------------------------------------------


def test_dashboard_index_renders(auth_client, clean_papers):
    client, _uid = auth_client
    r = client.get("/")
    assert r.status_code == 200


def test_dashboard_interest_add_and_remove(auth_client, clean_papers):
    client, uid = auth_client
    from app.core.models.user import User
    from app.modules.academic.service import list_user_keywords

    user = User.query.get(uid)

    client.post("/interests/add", data={"value": "graph neural networks"}, follow_redirects=False)
    kws = list_user_keywords(user)
    assert any(k.value == "graph neural networks" for k in kws)

    kw_id = next(k.id for k in kws if k.value == "graph neural networks")
    client.post(f"/interests/{kw_id}/delete", follow_redirects=False)
    assert not any(k.value == "graph neural networks" for k in list_user_keywords(user))
