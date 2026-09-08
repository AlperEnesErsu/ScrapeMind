"""Faz 5.2 — UserPage CRUD + ingestion tests."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape.service import (
    PAGE_CAP_MESSAGE,
    add_user_page,
    count_user_pages,
    get_user_page,
    ingest_user_pages,
    list_user_pages,
    remove_user_page,
    toggle_user_page,
)
from app.modules.scrape.sources.payload import PaperPayload
from app.modules.scrape.sources.web_source import PageFetchResult


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
        "scan_runs",
        "user_settings",
        "user_keywords",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="pageuser").delete()
    db.session.commit()
    u = User(
        username="pageuser",
        email="pageuser@example.test",
        full_name="Page User",
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
        "scan_runs",
        "user_settings",
        "user_keywords",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="pageuser").delete()
    db.session.commit()


def _page_payload(n: int = 1):
    return PaperPayload(
        source="user_page",
        external_id=f"https://news.example.test/post-{n}",
        title=f"Article {n}",
        abstract="Article summary text here.",
        authors=[],
        url=f"https://news.example.test/post-{n}",
        pdf_url=None,
        published_at=None,
        categories=["news"],
        kind="news",
    )


def _serve_page(monkeypatch, result: PageFetchResult):
    monkeypatch.setattr("app.modules.scrape.sources.web_source.discover", lambda url, **kw: result)


_OK_PAGE = PageFetchResult(
    payloads=[_page_payload(1)],
    status="ok",
    mode="blocks",
    etag="w/123",
    last_modified="Mon, 20 Jul 2026 00:00:00 GMT",
    title="Example News Site",
)


def test_add_user_page_succeeds_and_autofills_label_and_mode(app, db, clean_user, monkeypatch):
    _serve_page(monkeypatch, _OK_PAGE)
    with app.app_context():
        page, err = add_user_page(clean_user, "news.example.test/blog")
        assert err is None
        assert page is not None
        assert page.url == "https://news.example.test/blog"
        assert page.label == "Example News Site"
        assert page.mode == "blocks"
        assert page.active is True


def test_add_user_page_uses_explicit_label_and_selector(app, db, clean_user, monkeypatch):
    _serve_page(monkeypatch, _OK_PAGE)
    with app.app_context():
        page, err = add_user_page(
            clean_user,
            "https://news.example.test/blog",
            label="Custom Name",
            selector=".article-item",
        )
        assert err is None
        assert page.label == "Custom Name"
        assert page.selector == ".article-item"


def test_add_user_page_rejects_invalid_url(app, db, clean_user):
    with app.app_context():
        page, err = add_user_page(clean_user, "   ")
        assert page is None
        assert "valid page URL" in err


def test_add_user_page_rejects_robots_denied(app, db, clean_user, monkeypatch):
    denied = PageFetchResult(status="robots_denied", detail="Disallowed by /")
    _serve_page(monkeypatch, denied)
    with app.app_context():
        page, err = add_user_page(clean_user, "https://secret.example.test/page")
        assert page is None
        assert "robots.txt" in err


def test_add_user_page_rejects_empty_page(app, db, clean_user, monkeypatch):
    empty = PageFetchResult(status="empty", detail="Nothing recognisable on that page")
    _serve_page(monkeypatch, empty)
    with app.app_context():
        page, err = add_user_page(clean_user, "https://empty.example.test/page")
        assert page is None
        assert "Nothing recognisable" in err


def test_add_user_page_enforces_cap(app, db, clean_user, monkeypatch):
    _serve_page(monkeypatch, _OK_PAGE)
    with app.app_context():
        app.config["MAX_USER_PAGES"] = 3
        for i in range(3):
            p, err = add_user_page(clean_user, f"https://news.example.test/{i}")
            assert err is None
        p, err = add_user_page(clean_user, "https://news.example.test/overflow")
        assert p is None
        assert err == PAGE_CAP_MESSAGE


def test_add_user_page_rejects_private_host(app, db, clean_user, monkeypatch):
    monkeypatch.setitem(app.config, "FEED_ALLOW_PRIVATE_HOSTS", False)
    with app.app_context():
        for url in ("http://127.0.0.1/blog", "http://localhost/news"):
            page, err = add_user_page(clean_user, url)
            assert page is None
            assert err is not None


def test_add_user_page_is_idempotent_and_resumes(app, db, clean_user, monkeypatch):
    _serve_page(monkeypatch, _OK_PAGE)
    with app.app_context():
        p1, _ = add_user_page(clean_user, "https://news.example.test/page")
        p2, _ = add_user_page(clean_user, "https://news.example.test/page")
        assert p1.id == p2.id
        assert count_user_pages(clean_user) == 1

        toggle_user_page(clean_user, p1.id)
        assert get_user_page(clean_user, p1.id).active is False

        p3, _ = add_user_page(clean_user, "https://news.example.test/page")
        assert p3.active is True


def test_toggle_and_remove_user_page(app, db, clean_user, monkeypatch):
    _serve_page(monkeypatch, _OK_PAGE)
    with app.app_context():
        page, _ = add_user_page(clean_user, "https://news.example.test/page")
        assert page.active is True

        new_val = toggle_user_page(clean_user, page.id)
        assert new_val is False
        assert get_user_page(clean_user, page.id).active is False

        new_val = toggle_user_page(clean_user, page.id)
        assert new_val is True
        assert get_user_page(clean_user, page.id).active is True

        ok = remove_user_page(clean_user, page.id)
        assert ok is True
        assert get_user_page(clean_user, page.id) is None


def test_ingest_user_pages_upserts_and_records_validators(app, db, clean_user, monkeypatch):
    _serve_page(monkeypatch, _OK_PAGE)
    with app.app_context():
        page, _ = add_user_page(clean_user, "https://news.example.test/page")

        summary, touched = ingest_user_pages(clean_user)
        assert summary["hits"] == 1
        assert summary["new"] == 1
        assert len(touched) == 1
        assert touched[0].source == "user_page"

        reloaded = get_user_page(clean_user, page.id)
        assert reloaded.etag == "w/123"
        assert reloaded.last_modified == "Mon, 20 Jul 2026 00:00:00 GMT"
        assert reloaded.last_scraped_at is not None


def test_page_add_remove_toggle_cycle_over_http(auth_client, db, monkeypatch):
    client, uid = auth_client
    _serve_page(monkeypatch, _OK_PAGE)

    r = client.post(
        "/papers/profile/pages/add",
        data={"url": "https://news.example.test/blog", "label": "", "selector": ""},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Example News Site" in body

    from app.core.models.user import User

    user = User.query.get(uid)
    pages = list_user_pages(user)
    assert len(pages) == 1
    page_id = pages[0].id

    # Toggle off
    r = client.post(
        f"/papers/profile/pages/{page_id}/toggle",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert list_user_pages(user)[0].active is False

    # Remove
    r = client.post(
        f"/papers/profile/pages/{page_id}/remove",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert list_user_pages(user) == []

    # Removing again -> 404
    r = client.post(f"/papers/profile/pages/{page_id}/remove")
    assert r.status_code == 404


def test_page_list_filter_chips(auth_client, db, monkeypatch):
    client, uid = auth_client
    _serve_page(monkeypatch, _OK_PAGE)

    from app.core.models.user import User
    from app.modules.scrape.service import add_user_page, toggle_user_page

    user = User.query.get(uid)
    p1, _ = add_user_page(user, "https://news1.example.test/blog", label="Active Page")
    p2, _ = add_user_page(user, "https://news2.example.test/blog", label="Paused Page")
    toggle_user_page(user, p2.id)

    r = client.get("/papers/profile/pages?filter=all")
    assert r.status_code == 200
    b = r.get_data(as_text=True)
    assert "Active Page" in b
    assert "Paused Page" in b

    r = client.get("/papers/profile/pages?filter=active")
    b = r.get_data(as_text=True)
    assert "Active Page" in b
    assert "Paused Page" not in b

    r = client.get("/papers/profile/pages?filter=paused")
    b = r.get_data(as_text=True)
    assert "Active Page" not in b
    assert "Paused Page" in b
