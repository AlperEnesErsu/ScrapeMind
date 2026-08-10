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
        ("get", "/papers/profile/source-manager"),
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

    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(
        "app.modules.scrape.ai_service.ask_paper",
        lambda paper, question, history=None, user=None: "Bu bir test cevabıdır.",
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
    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda user=None: True)
    r = client.post(f"/papers/{link.id}/chat", data={"message": "   "})
    assert r.status_code == 204


def test_chat_blocked_when_ai_disabled(auth_client, clean_papers, monkeypatch):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda user=None: False)
    r = client.post(f"/papers/{link.id}/chat", data={"message": "Merhaba"})
    assert r.status_code == 400


def test_chat_escapes_user_input(auth_client, clean_papers, monkeypatch):
    """A question containing HTML must come back escaped, not as live markup —
    guards the stored-XSS hole in the chat response."""
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)

    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(
        "app.modules.scrape.ai_service.ask_paper",
        lambda paper, question, history=None, user=None: "cevap",
    )

    r = client.post(
        f"/papers/{link.id}/chat",
        data={"message": "<script>alert('xss')</script>"},
    )
    body = r.get_data(as_text=True)
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


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


def test_interest_add_htmx_returns_partial(auth_client, clean_papers):
    """HX-Request add returns the interests-manager partial (no full redirect)."""
    client, _uid = auth_client
    r = client.post(
        "/interests/add",
        data={"value": "diffusion models"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="interests-manager"' in body
    assert "diffusion models" in body


def test_scrape_now_collapses_duplicate_presses(auth_client, clean_papers, monkeypatch):
    """Second 'Scrape now' while one is in flight returns the already-running
    widget instead of queuing another task."""
    client, _uid = auth_client

    # First press: lock free → spinner (task queued; user has no keywords so the
    # eager task no-ops without hitting any external API).
    monkeypatch.setattr("app.modules.scrape.service.acquire_scrape_lock", lambda user_id: True)
    r1 = client.post("/papers/run", headers={"HX-Request": "true"})
    assert r1.status_code == 200

    # Duplicate press: lock held → already-running widget, no new task.
    monkeypatch.setattr("app.modules.scrape.service.acquire_scrape_lock", lambda user_id: False)
    r2 = client.post("/papers/run", headers={"HX-Request": "true"})
    assert r2.status_code == 200
    assert 'id="scraper-widget"' in r2.get_data(as_text=True)


def test_toggle_source_mutes_and_persists(auth_client, clean_papers):
    client, uid = auth_client
    from app.core.models.user import User
    from app.modules.scrape.service import list_user_source_prefs

    user = User.query.get(uid)

    # Mute arxiv via HTMX toggle → get the sources-card partial back
    r = client.post(
        "/sources/toggle",
        data={"source_name": "arxiv", "enabled": "false"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert 'id="sources-card"' in r.get_data(as_text=True)
    assert list_user_source_prefs(user).get("arxiv") is False

    # Unknown source is rejected
    r = client.post(
        "/sources/toggle",
        data={"source_name": "not_a_source", "enabled": "false"},
    )
    assert r.status_code == 400


def test_toggle_source_works_for_rss_feed_source(auth_client, clean_papers):
    """RSS feeds (Faz 2) register into the same opt-out source registry as
    the academic adapters — muting one must not require special-casing."""
    client, uid = auth_client
    from app.core.models.user import User
    from app.modules.scrape.service import list_user_source_prefs

    user = User.query.get(uid)
    r = client.post(
        "/sources/toggle",
        data={"source_name": "openai_blog", "enabled": "false"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert list_user_source_prefs(user).get("openai_blog") is False


def test_dashboard_sources_card_lists_feed_sources(auth_client, clean_papers):
    client, _uid = auth_client
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert "OpenAI Blog" in body


# --------------------------------------------------------------------------
# Faz 3 — interest-aware sources-card grouping render
# --------------------------------------------------------------------------


def test_sources_card_groups_feed_into_other_without_matching_interest(auth_client, clean_papers):
    """httptester has no interests -> topics=["general"] -> no feed overlaps
    -> every curated RSS feed lands in "Diğer kaynaklar" (Other sources),
    after the "Önerilen" (Suggested) heading (if present) rather than before
    it. Section headers render in Turkish (BABEL_DEFAULT_LOCALE=tr in tests)."""
    client, _uid = auth_client
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert "Diğer kaynaklar" in body
    other_idx = body.index("Diğer kaynaklar")
    openai_idx = body.index("OpenAI Blog")
    assert openai_idx > other_idx


def test_sources_card_suggests_feed_for_matching_interest(auth_client, clean_papers):
    """Adding an AI-lexicon interest reclassifies the user to topic "ai" ->
    the curated AI feeds should now render inside the "Önerilen" (Suggested)
    section, ahead of the "Diğer kaynaklar" (Other sources) heading."""
    client, uid = auth_client
    from app.core.models.user import User

    user = User.query.get(uid)
    client.post("/interests/add", data={"value": "yapay zeka"}, follow_redirects=False)

    from app.modules.scrape import ai_service

    assert ai_service.classify_user_topics(user) == ["ai"]

    r = client.get("/")
    body = r.get_data(as_text=True)
    assert "Önerilen (ilgine uygun)" in body
    suggested_idx = body.index("Önerilen (ilgine uygun)")
    openai_idx = body.index("OpenAI Blog")
    other_idx = body.index("Diğer kaynaklar") if "Diğer kaynaklar" in body else len(body)
    assert suggested_idx < openai_idx < other_idx


# --------------------------------------------------------------------------
# Papers feed (/papers) — sources panel + read-only interests strip
# --------------------------------------------------------------------------


def test_papers_feed_renders_sources_panel_and_interests_strip(auth_client, clean_papers):
    """scrape.feed (`sources_card_context` extracted from dashboard's
    `_sources_context`) shows the same sources card as the dashboard, plus a
    read-only strip of the user's interest keywords."""
    client, _uid = auth_client
    client.post("/interests/add", data={"value": "transformer"}, follow_redirects=False)

    r = client.get("/papers/")
    body = r.get_data(as_text=True)

    # Interest keyword shows up as an info pill.
    assert "transformer" in body
    # Sources card rendered (an always-on academic source shows its label).
    assert "arXiv" in body
    assert 'id="sources-card"' in body


# --------------------------------------------------------------------------
# Faz 3 — AI/Kaynaklar profile tab: custom RSS feed CRUD over HTTP
# --------------------------------------------------------------------------


def test_ai_tab_renders_topics_and_empty_feed_state(auth_client, clean_papers):
    client, _uid = auth_client
    r = client.get("/settings/profile?tab=ai")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Turkish translations (BABEL_DEFAULT_LOCALE=tr in tests) —
    # "Your own RSS feeds" / "No custom feeds yet — add one below."
    assert "Kendi RSS kaynakların" in body
    assert "Henüz kendi RSS kaynağın yok" in body


def _stub_feed_fetch(monkeypatch, result):
    """`add_user_feed` validates by fetching once — stub that fetch."""
    monkeypatch.setattr(
        "app.modules.scrape.service.fetch_feed_conditional", lambda feed, **kw: result
    )


def _ok_fetch_result(title="Example Blog"):
    from app.modules.scrape.sources.payload import PaperPayload
    from app.modules.scrape.sources.rss_source import FeedFetchResult

    payload = PaperPayload(
        source="user_feed",
        external_id="1",
        title="Post 1",
        abstract=None,
        authors=[],
        url="https://blog.example.test/1",
        pdf_url=None,
        published_at=None,
        categories=["announcement"],
        kind="news",
    )
    return FeedFetchResult([payload], "ok", title=title)


def test_feed_add_remove_toggle_cycle_over_http(auth_client, clean_papers, monkeypatch):
    client, uid = auth_client
    _stub_feed_fetch(monkeypatch, _ok_fetch_result())

    r = client.post(
        "/papers/profile/feeds/add",
        data={"url": "https://blog.example.test/feed.xml", "label": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Example Blog" in body

    from app.core.models.user import User
    from app.modules.scrape.service import list_user_feeds

    user = User.query.get(uid)
    feeds = list_user_feeds(user)
    assert len(feeds) == 1
    feed_id = feeds[0].id

    # Toggle off
    r = client.post(
        f"/papers/profile/feeds/{feed_id}/toggle",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert list_user_feeds(user)[0].active is False

    # Remove
    r = client.post(
        f"/papers/profile/feeds/{feed_id}/remove",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert list_user_feeds(user) == []

    # Removing again -> 404
    r = client.post(f"/papers/profile/feeds/{feed_id}/remove")
    assert r.status_code == 404


def test_feed_add_rejects_invalid_url_over_http(auth_client, clean_papers, monkeypatch):
    """A scheme-only URL (no host) fails normalization before any fetch
    attempt — guard the fetch with an assertion so this never risks a
    real network call even if normalization regresses. (A blank/whitespace
    field would instead fail WTForms' DataRequired at the form level, a
    different — also valid — rejection path exercised implicitly by the
    "Please correct the errors below" flow, not this one.)"""
    client, _uid = auth_client

    def _boom(*a, **k):
        raise AssertionError("must not fetch for an unparseable URL")

    monkeypatch.setattr("app.modules.scrape.service.fetch_feed_conditional", _boom)

    r = client.post(
        "/papers/profile/feeds/add",
        data={"url": "https://", "label": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Turkish translation of "Please enter a valid feed URL (...)."
    assert "kaynak URL" in body


def test_feed_add_rejects_unparseable_feed_over_http(auth_client, clean_papers, monkeypatch):
    """A syntactically valid URL whose feed can't be fetched (stubbed
    failure, no real network) is rejected with the "could not read" error."""
    from app.modules.scrape.sources.rss_source import FeedFetchResult

    client, _uid = auth_client
    _stub_feed_fetch(monkeypatch, FeedFetchResult([], "timeout"))

    r = client.post(
        "/papers/profile/feeds/add",
        data={"url": "https://blog.example.test/broken.xml", "label": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Turkish translation of "Could not read that feed — check the URL..."
    assert "okunamadı" in body


def test_feed_toggle_swaps_only_the_feed_list(auth_client, clean_papers, monkeypatch):
    """A toggle must return the feed-list partial, not the whole AI tab.

    Re-rendering the tab re-runs `_ai_ctx()` -> `classify_user_topics`, i.e. an
    LLM round trip on a checkbox click. Asserting on the response body is the
    only way to keep that from creeping back.
    """
    from app.core.models.user import User
    from app.modules.scrape.service import list_user_feeds

    client, uid = auth_client
    _stub_feed_fetch(monkeypatch, _ok_fetch_result())
    client.post(
        "/papers/profile/feeds/add",
        data={"url": "https://blog.example.test/feed.xml", "label": ""},
        headers={"HX-Request": "true"},
    )
    feed_id = list_user_feeds(User.query.get(uid))[0].id

    def _boom(user):
        raise AssertionError("a feed toggle must not trigger topic classification")

    monkeypatch.setattr("app.modules.scrape.ai_service.classify_user_topics", _boom)

    r = client.post(f"/papers/profile/feeds/{feed_id}/toggle", headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="feed-list"' in body
    # The API-key section belongs to the full tab and must not come back here
    assert "OpenRouter" not in body


def test_feed_list_filter_chips(auth_client, clean_papers, monkeypatch):
    from app.core.models.user import User
    from app.modules.scrape.service import list_user_feeds

    client, uid = auth_client
    _stub_feed_fetch(monkeypatch, _ok_fetch_result())
    for i in range(2):
        client.post(
            "/papers/profile/feeds/add",
            data={"url": f"https://blog.example.test/{i}.xml", "label": f"Feed {i}"},
            headers={"HX-Request": "true"},
        )
    feeds = list_user_feeds(User.query.get(uid))
    assert len(feeds) == 2
    client.post(f"/papers/profile/feeds/{feeds[0].id}/toggle", headers={"HX-Request": "true"})

    active = client.get("/papers/profile/feeds?filter=active").get_data(as_text=True)
    paused = client.get("/papers/profile/feeds?filter=paused").get_data(as_text=True)
    assert feeds[1].label in active and feeds[0].label not in active
    assert feeds[0].label in paused and feeds[1].label not in paused


def test_feed_add_rejects_over_the_cap(auth_client, clean_papers, monkeypatch, app):
    """The cap is what keeps "hundreds of RSS feeds" from becoming hundreds of
    HTTP requests per user per night."""
    client, _uid = auth_client
    _stub_feed_fetch(monkeypatch, _ok_fetch_result())
    monkeypatch.setitem(app.config, "MAX_USER_FEEDS", 2)

    for i in range(2):
        r = client.post(
            "/papers/profile/feeds/add",
            data={"url": f"https://blog.example.test/{i}.xml", "label": ""},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200

    r = client.post(
        "/papers/profile/feeds/add",
        data={"url": "https://blog.example.test/too-many.xml", "label": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    # Turkish translation of "Feed limit reached. Remove one before adding another."
    assert "sınırına ulaştın" in r.get_data(as_text=True)


# --------------------------------------------------------------------------
# Faz 3 — AI/Kaynaklar profile tab: YouTube channel subscription CRUD over
# HTTP. Twin of the custom-feed tests above; `resolve_channel` is stubbed at
# its own source module (never real network) since add_user_channel imports
# it locally rather than importing it into service.py's module namespace.
# --------------------------------------------------------------------------


def _stub_resolve_channel(monkeypatch, result):
    """`add_user_channel` validates by resolving once — stub that resolution.

    `result` is either a `(dict, None)` success tuple or a `(None, error)`
    failure tuple, matching `resolve_channel`'s own return contract.
    """
    monkeypatch.setattr(
        "app.modules.scrape.sources.youtube_channel_source.resolve_channel",
        lambda raw: result,
    )


def _ok_resolved(channel_id="UC1111111111111111111111", title="Example Channel"):
    return (
        {
            "channel_id": channel_id,
            "title": title,
            "url": f"https://www.youtube.com/channel/{channel_id}",
        },
        None,
    )


def test_channel_add_remove_toggle_cycle_over_http(auth_client, clean_papers, monkeypatch):
    client, uid = auth_client
    _stub_resolve_channel(monkeypatch, _ok_resolved())

    r = client.post(
        "/papers/profile/channels/add",
        data={"url": "@examplechannel", "label": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Example Channel" in body

    from app.core.models.user import User
    from app.modules.scrape.service import list_user_channels

    user = User.query.get(uid)
    channels = list_user_channels(user)
    assert len(channels) == 1
    channel_id = channels[0].id

    # Toggle off
    r = client.post(
        f"/papers/profile/channels/{channel_id}/toggle",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert list_user_channels(user)[0].active is False

    # Remove
    r = client.post(
        f"/papers/profile/channels/{channel_id}/remove",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert list_user_channels(user) == []

    # Removing again -> 404
    r = client.post(f"/papers/profile/channels/{channel_id}/remove")
    assert r.status_code == 404


def test_channel_add_rejects_resolution_failure_over_http(auth_client, clean_papers, monkeypatch):
    client, _uid = auth_client
    _stub_resolve_channel(
        monkeypatch,
        (None, "Could not find that YouTube channel — check the link and try again."),
    )

    r = client.post(
        "/papers/profile/channels/add",
        data={"url": "@doesnotexist", "label": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Turkish translation of "Could not find that YouTube channel — ..."
    assert "bulunamadı" in body


def test_channel_toggle_swaps_only_the_channel_list(auth_client, clean_papers, monkeypatch):
    """A toggle must return the channel-list partial, not the whole AI tab —
    same reasoning as the feed toggle: re-rendering the tab re-runs
    `_ai_ctx()` -> `classify_user_topics`, an LLM round trip on a checkbox
    click.
    """
    from app.core.models.user import User
    from app.modules.scrape.service import list_user_channels

    client, uid = auth_client
    _stub_resolve_channel(monkeypatch, _ok_resolved())
    client.post(
        "/papers/profile/channels/add",
        data={"url": "@examplechannel", "label": ""},
        headers={"HX-Request": "true"},
    )
    channel_id = list_user_channels(User.query.get(uid))[0].id

    def _boom(user):
        raise AssertionError("a channel toggle must not trigger topic classification")

    monkeypatch.setattr("app.modules.scrape.ai_service.classify_user_topics", _boom)

    r = client.post(f"/papers/profile/channels/{channel_id}/toggle", headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="channel-list"' in body
    # The API-key section belongs to the full tab and must not come back here
    assert "OpenRouter" not in body


def test_channel_list_filter_chips(auth_client, clean_papers, monkeypatch):
    from app.core.models.user import User
    from app.modules.scrape.service import list_user_channels

    client, uid = auth_client
    for i in range(2):
        _stub_resolve_channel(
            monkeypatch, _ok_resolved(channel_id=f"UC{i:024d}", title=f"Channel {i}")
        )
        client.post(
            "/papers/profile/channels/add",
            data={"url": f"@channel{i}", "label": f"Channel {i}"},
            headers={"HX-Request": "true"},
        )
    channels = list_user_channels(User.query.get(uid))
    assert len(channels) == 2
    client.post(f"/papers/profile/channels/{channels[0].id}/toggle", headers={"HX-Request": "true"})

    active = client.get("/papers/profile/channels?filter=active").get_data(as_text=True)
    paused = client.get("/papers/profile/channels?filter=paused").get_data(as_text=True)
    assert channels[1].title in active and channels[0].title not in active
    assert channels[0].title in paused and channels[1].title not in paused


def test_channel_add_rejects_over_the_cap(auth_client, clean_papers, monkeypatch, app):
    """The cap is what keeps "dozens of channel subscriptions" from becoming
    dozens of nightly feed fetches (plus transcript/summary work) per user."""
    client, _uid = auth_client
    monkeypatch.setitem(app.config, "MAX_USER_CHANNELS", 2)

    for i in range(2):
        _stub_resolve_channel(
            monkeypatch, _ok_resolved(channel_id=f"UC{i:024d}", title=f"Channel {i}")
        )
        r = client.post(
            "/papers/profile/channels/add",
            data={"url": f"@channel{i}", "label": ""},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200

    _stub_resolve_channel(monkeypatch, _ok_resolved(channel_id="UC" + "9" * 24, title="Too Many"))
    r = client.post(
        "/papers/profile/channels/add",
        data={"url": "@toomanychannel", "label": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    # Turkish translation of "Channel limit reached. Remove one before adding another."
    assert "sınırına ulaştın" in r.get_data(as_text=True)


# --------------------------------------------------------------------------
# Home-page source manager modal — GET /papers/profile/source-manager and
# the surface-aware add-feed/add-channel routes
# --------------------------------------------------------------------------


def test_source_manager_route_renders_feed_and_channel_lists(auth_client, clean_papers):
    client, _uid = auth_client
    r = client.get("/papers/profile/source-manager")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="feed-list"' in body
    assert 'id="channel-list"' in body


def test_source_manager_route_skips_topic_classification(auth_client, clean_papers, monkeypatch):
    """The modal opens on every click (unlike the settings tab, which the
    user navigates to deliberately) — it must never resolve topics via an
    LLM call, only the cheap feed/channel DB lookups. Mirrors
    test_feed_toggle_swaps_only_the_feed_list's `_boom` guard."""
    client, _uid = auth_client

    def _boom(user):
        raise AssertionError("opening the source manager must not classify topics")

    monkeypatch.setattr("app.modules.scrape.ai_service.classify_user_topics", _boom)

    r = client.get("/papers/profile/source-manager")
    assert r.status_code == 200


def test_feed_add_modal_surface_returns_manager_partial_not_tab(
    auth_client, clean_papers, monkeypatch
):
    client, _uid = auth_client
    _stub_feed_fetch(monkeypatch, _ok_fetch_result())

    r = client.post(
        "/papers/profile/feeds/add",
        data={"url": "https://blog.example.test/feed.xml", "label": "", "surface": "modal"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="feed-list"' in body
    assert 'id="channel-list"' in body
    # The API-key section belongs to the full settings tab only — its
    # presence would mean this fell through to _render_settings_tab instead
    # of the cheap modal partial.
    assert "OpenRouter" not in body


def test_channel_add_over_cap_modal_surface_shows_flash(
    auth_client, clean_papers, monkeypatch, app
):
    """The cap message must survive in the modal surface too, not just the
    settings tab — that's the whole reason submit_channel_add swaps a
    wrapper instead of just the list (see the comment above the add-form in
    settings/_source_manager.html)."""
    client, _uid = auth_client
    monkeypatch.setitem(app.config, "MAX_USER_CHANNELS", 1)

    _stub_resolve_channel(monkeypatch, _ok_resolved(channel_id="UC" + "1" * 24, title="First"))
    r = client.post(
        "/papers/profile/channels/add",
        data={"url": "@examplechannel", "label": "", "surface": "modal"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200

    _stub_resolve_channel(monkeypatch, _ok_resolved(channel_id="UC" + "2" * 24, title="Too Many"))
    r = client.post(
        "/papers/profile/channels/add",
        data={"url": "@toomanychannel", "label": "", "surface": "modal"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Turkish translation of "Channel limit reached. Remove one before adding another."
    assert "sınırına ulaştın" in body
    assert 'id="channel-list"' in body


def test_source_manager_modal_renders_once_on_dashboard_and_feed(auth_client, clean_papers):
    """The modal shell must be a sibling of #sources-card, included once per
    page — not once per card re-render, and not duplicated if some future
    edit accidentally includes it twice."""
    client, _uid = auth_client
    r = client.get("/")
    assert r.get_data(as_text=True).count('id="sourceManagerModal"') == 1

    r = client.get("/papers/")
    assert r.get_data(as_text=True).count('id="sourceManagerModal"') == 1


def test_source_manager_modal_backdrop_is_static(auth_client, clean_papers):
    """An outside click must not silently close the modal and drop whatever
    the user had half-typed into the feed/channel URL field — Esc stays a
    valid way out (data-bs-keyboard untouched), only the stray-click dismiss
    is disabled."""
    client, _uid = auth_client
    body = client.get("/").get_data(as_text=True)
    assert 'id="sourceManagerModal"' in body
    assert 'data-bs-backdrop="static"' in body


def test_channel_add_modal_surface_activates_channels_pane(auth_client, clean_papers, monkeypatch):
    """Adding a channel re-renders the whole source-manager partial (see
    submit_channel_add -> _render_source_manager_result), which would reset
    to the first tab unless `active_pane` threads through — assert the
    channels tab comes back active, not feeds."""
    client, _uid = auth_client
    _stub_resolve_channel(monkeypatch, _ok_resolved())

    r = client.post(
        "/papers/profile/channels/add",
        data={"url": "@examplechannel", "label": "", "surface": "modal"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'nav-link active" id="source-manager-tab-channels"' in body
    assert 'nav-link active" id="source-manager-tab-feeds"' not in body


def test_feed_add_modal_surface_activates_feeds_pane(auth_client, clean_papers, monkeypatch):
    """Twin of the channel-add case above, other direction: adding a feed
    must come back with the feeds tab active."""
    client, _uid = auth_client
    _stub_feed_fetch(monkeypatch, _ok_fetch_result())

    r = client.post(
        "/papers/profile/feeds/add",
        data={"url": "https://blog.example.test/feed.xml", "label": "", "surface": "modal"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'nav-link active" id="source-manager-tab-feeds"' in body
    assert 'nav-link active" id="source-manager-tab-channels"' not in body


def test_settings_tab_source_manager_has_no_tab_strip(auth_client, clean_papers):
    """The settings tab is a full page with room to stack both sections —
    tabs are a modal-only affordance, changing the tab surface would be an
    unrequested regression."""
    client, _uid = auth_client
    r = client.get("/settings/profile?tab=ai")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="feed-list"' in body
    assert 'id="channel-list"' in body
    assert "source-manager-tabs" not in body
    assert "nav-tabs" not in body


def test_home_page_shows_add_source_button_and_honest_scanned_count(
    auth_client, clean_papers, monkeypatch
):
    """The profile-summary pill must count sources that actually get scanned
    — curated sources plus the user's own active feeds/channels — not just
    active_source_count (curated only). A small icon-only button next to it
    opens the same source-manager modal as the sources card's full link."""
    import re

    client, _uid = auth_client

    r = client.get("/")
    body = r.get_data(as_text=True)
    # Icon-only shortcut, accessible name via title/aria-label (Turkish
    # translation of "Manage your sources", the modal's own title).
    assert 'data-bs-target="#sourceManagerModal"' in body
    assert "Kaynaklarını yönet" in body

    match = re.search(r"(\d+)\s*aktif kaynak", body)
    assert match is not None
    baseline = int(match.group(1))

    _stub_feed_fetch(monkeypatch, _ok_fetch_result())
    client.post(
        "/papers/profile/feeds/add",
        data={"url": "https://blog.example.test/feed.xml", "label": ""},
        headers={"HX-Request": "true"},
    )
    _stub_resolve_channel(monkeypatch, _ok_resolved())
    client.post(
        "/papers/profile/channels/add",
        data={"url": "@examplechannel", "label": ""},
        headers={"HX-Request": "true"},
    )

    body2 = client.get("/").get_data(as_text=True)
    match2 = re.search(r"(\d+)\s*aktif kaynak", body2)
    assert match2 is not None
    # One active feed + one active channel, on top of whatever curated
    # sources were already counted.
    assert int(match2.group(1)) == baseline + 2


# --------------------------------------------------------------------------
# Video transcript summaries — card TL;DR, detail panel, generation route
# --------------------------------------------------------------------------


def _make_video_paper(
    ext_id: str = "yt-000001", *, title: str = "A Great Conference Talk"
) -> Paper:
    paper = Paper(
        source="youtube_channel",
        external_id=ext_id,
        title=title,
        abstract="Video description provided by the channel.",
        authors=["Some Channel"],
        url=f"https://www.youtube.com/watch?v={ext_id}",
        pdf_url=None,
        published_at=datetime(2026, 1, 15, tzinfo=UTC),
        categories=["video"],
        kind="video",
    )
    _db.session.add(paper)
    _db.session.commit()
    return paper


def test_detail_page_shows_video_summary_panel_for_video_kind(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_video_paper()
    link = _link(uid, paper)
    r = client.get(f"/papers/{link.id}?mode=ai")
    assert r.status_code == 200
    assert 'id="video-summary-panel"' in r.get_data(as_text=True)


def test_detail_page_hides_video_summary_panel_for_normal_paper(auth_client, clean_papers):
    client, uid = auth_client
    paper = _make_paper()
    link = _link(uid, paper)
    r = client.get(f"/papers/{link.id}?mode=ai")
    assert r.status_code == 200
    assert 'id="video-summary-panel"' not in r.get_data(as_text=True)


def test_card_renders_tldr_and_badge_when_video_summary_exists(auth_client, clean_papers):
    """The card must show the VideoSummary's tldr (with an AI badge), not
    the raw video description, once a summary has been generated."""
    client, uid = auth_client
    paper = _make_video_paper()
    _link(uid, paper)

    from app.modules.scrape.models import VideoSummary

    _db.session.add(VideoSummary(paper_id=paper.id, tldr="Kısa ve öz bir video özeti."))
    _db.session.commit()

    r = client.get("/papers/")
    body = r.get_data(as_text=True)
    assert "Kısa ve öz bir video özeti." in body
    assert "Video description provided by the channel." not in body
    # Turkish translation of "AI summary" (BABEL_DEFAULT_LOCALE=tr in tests)
    assert "Yapay zeka özeti" in body


def test_card_renders_plain_abstract_when_no_video_summary(auth_client, clean_papers):
    """No VideoSummary row yet → the card falls back to the raw description,
    same as any other paper."""
    client, uid = auth_client
    paper = _make_video_paper()
    _link(uid, paper)

    r = client.get("/papers/")
    body = r.get_data(as_text=True)
    assert "Video description provided by the channel." in body
    assert "Yapay zeka özeti" not in body


def test_video_summary_route_renders_generated_summary(auth_client, clean_papers, monkeypatch):
    client, uid = auth_client
    paper = _make_video_paper()
    link = _link(uid, paper)

    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(
        "app.modules.scrape.sources.youtube_channel_source.fetch_transcript",
        lambda video_id, **kw: "a raw transcript",
    )

    from app.modules.scrape.models import VideoSummary

    def _fake_generate(paper, transcript, **kw):
        s = VideoSummary(
            paper_id=paper.id,
            tldr="Üretilen sahte özet",
            highlights=["önemli nokta"],
            topics=["ai"],
            transcript_chars=len(transcript or ""),
            model_version="test-model",
        )
        _db.session.add(s)
        _db.session.commit()
        return s

    monkeypatch.setattr("app.modules.scrape.ai_service.generate_video_summary", _fake_generate)

    r = client.post(f"/papers/{link.id}/video-summary", headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Üretilen sahte özet" in body


def test_video_summary_route_no_transcript_state(auth_client, clean_papers, monkeypatch):
    """fetch_transcript returning None (no captions) must render the distinct
    'no transcript' state, not crash, and not silently look like the
    'never tried' empty state."""
    client, uid = auth_client
    paper = _make_video_paper()
    link = _link(uid, paper)

    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(
        "app.modules.scrape.sources.youtube_channel_source.fetch_transcript",
        lambda video_id, **kw: None,
    )
    monkeypatch.setattr(
        "app.modules.scrape.ai_service.generate_video_summary",
        lambda paper, transcript, **kw: None,
    )

    r = client.post(f"/papers/{link.id}/video-summary", headers={"HX-Request": "true"})
    assert r.status_code == 200
    # Turkish translation of "No transcript available"
    assert "Transkript bulunamadı" in r.get_data(as_text=True)


def test_video_summary_route_ai_disabled_skips_transcript_fetch(
    auth_client, clean_papers, monkeypatch
):
    """AI disabled must short-circuit before ever shelling out to yt-dlp —
    fetch_transcript spawns a subprocess and must not run for nothing."""
    client, uid = auth_client
    paper = _make_video_paper()
    link = _link(uid, paper)

    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda user=None: False)

    def _boom(*a, **k):
        raise AssertionError("must not shell out to yt-dlp when AI is disabled")

    monkeypatch.setattr("app.modules.scrape.sources.youtube_channel_source.fetch_transcript", _boom)

    r = client.post(f"/papers/{link.id}/video-summary", headers={"HX-Request": "true"})
    assert r.status_code == 200
    # Turkish translation of "AI is not configured"
    assert "AI yapılandırılmadı" in r.get_data(as_text=True)


def test_video_summary_route_ownership_guard(auth_client, clean_papers, db):
    """A user cannot POST to someone else's paper — mirrors the 404 guard
    every other /papers/<id>/... route already enforces."""
    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.user import User

    client, uid = auth_client
    paper = _make_video_paper()
    link = _link(uid, paper)

    db.session.query(User).filter_by(username="video_summary_other").delete()
    db.session.commit()
    other = User(
        username="video_summary_other",
        email="video_summary_other@example.test",
        full_name="Other User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(other)
    db.session.commit()

    # Same test client, switched to the second user's session — matches the
    # pattern in tests/modules/test_library_export.py's ownership check.
    with client.session_transaction() as sess:
        sess["_user_id"] = str(other.id)
        sess["_fresh"] = True

    r = client.post(f"/papers/{link.id}/video-summary", headers={"HX-Request": "true"})
    assert r.status_code == 404

    db.session.query(User).filter_by(username="video_summary_other").delete()
    db.session.commit()


def _count_queries(fn):
    """Count SQL statements executed by `fn()` via a `before_cursor_execute`
    listener on the app's engine — the suite has no existing query-counting
    helper, so this is deliberately minimal: attach, run, detach, return the
    tally. Good enough to catch an N+1 without pulling in a profiling lib."""
    from sqlalchemy import event

    count = 0

    def _listener(*_args, **_kwargs):
        nonlocal count
        count += 1

    event.listen(_db.engine, "before_cursor_execute", _listener)
    try:
        fn()
    finally:
        event.remove(_db.engine, "before_cursor_execute", _listener)
    return count


def test_feed_video_summary_query_count_does_not_scale_with_card_count(auth_client, clean_papers):
    """`_paper_card.html` reads `r.paper.video_summary` per card. Without the
    joinedload added to `list_user_papers`, that's one extra SELECT per row
    (N+1) — this compares the query count for a small feed against a larger
    one and asserts they're equal, which an N+1 would fail (the second count
    would grow with the row count)."""
    client, uid = auth_client

    from app.modules.scrape.models import VideoSummary

    def _add_video_papers(n, offset):
        for i in range(offset, offset + n):
            p = _make_video_paper(f"yt-{i:06d}", title=f"Video number {i}")
            _link(uid, p)
            _db.session.add(VideoSummary(paper_id=p.id, tldr=f"Özet {i}"))
        _db.session.commit()

    _add_video_papers(2, offset=0)
    small_count = _count_queries(lambda: client.get("/papers/"))

    _add_video_papers(6, offset=2)
    large_count = _count_queries(lambda: client.get("/papers/"))

    assert small_count == large_count, (
        f"query count grew with row count ({small_count} -> {large_count}) — "
        "looks like an N+1 on Paper.video_summary"
    )
