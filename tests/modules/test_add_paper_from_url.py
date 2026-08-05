"""Tests for manually adding papers from URLs (add_paper_from_url)."""

from __future__ import annotations

import pytest

from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.modules.scrape.models import Paper, UserPaper
from app.modules.scrape.service import add_paper_from_url
from app.modules.scrape.sources.payload import PaperPayload


@pytest.fixture
def a_user(db):
    user = User.query.filter_by(email="linkuser@example.test").first()
    if user:
        return user
    user = User(
        username="linkuser",
        email="linkuser@example.test",
        full_name="Link User",
        password_hash=generate_password_hash("password123"),
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_add_paper_from_url_valid(db, a_user, monkeypatch):
    """Verify add_paper_from_url creates a Paper with source=manual and kind=link."""

    def mock_search_web(url):
        return [
            PaperPayload(
                source="web_reach",
                external_id="web:https://example.com/article",
                title="Sample Article Title",
                abstract="Clean plain text abstract of the article.",
                authors=["Web Reader"],
                url=url,
                pdf_url=None,
                published_at=None,
                categories=["web"],
                kind="news",
            )
        ]

    monkeypatch.setattr(
        "app.modules.scrape.sources.external_sources.search_web", mock_search_web
    )

    link, created = add_paper_from_url(a_user, "https://example.com/article")
    assert created is True
    assert isinstance(link, UserPaper)
    assert link.paper.source == "manual"
    assert link.paper.kind == "link"
    assert link.paper.title == "Sample Article Title"
    assert link.paper.url == "https://example.com/article"
    assert link.matched_keyword == "elle eklendi"


def test_add_paper_from_url_net_guard_blocking(db, a_user):
    """Verify add_paper_from_url raises ValueError when net_guard blocks private IP."""
    with pytest.raises(ValueError):
        add_paper_from_url(a_user, "http://127.0.0.1:6379/")


def test_add_paper_from_url_deduplication(db, a_user, monkeypatch):
    """Verify adding the same URL twice is idempotent and returns created=False."""

    def mock_search_web(url):
        return [
            PaperPayload(
                source="web_reach",
                external_id="web:https://example.com/dedup",
                title="Dedup Article",
                abstract="Abstract content.",
                authors=["Author"],
                url=url,
                pdf_url=None,
                published_at=None,
                categories=["web"],
                kind="news",
            )
        ]

    monkeypatch.setattr(
        "app.modules.scrape.sources.external_sources.search_web", mock_search_web
    )

    link1, created1 = add_paper_from_url(a_user, "https://example.com/dedup")
    assert created1 is True

    link2, created2 = add_paper_from_url(a_user, "https://example.com/dedup")
    assert created2 is False
    assert link1.id == link2.id


def test_add_paper_from_url_unreachable(db, a_user, monkeypatch):
    """Verify add_paper_from_url raises ValueError if content fetch fails."""

    def mock_search_web(url):
        return []

    monkeypatch.setattr(
        "app.modules.scrape.sources.external_sources.search_web", mock_search_web
    )

    with pytest.raises(ValueError):
        add_paper_from_url(a_user, "https://example.com/404-not-found")


def test_add_link_route_htmx(client, a_user, db, monkeypatch):
    """Test POST /papers/add-link route via HTMX."""
    from flask_login import login_user

    def mock_search_web(url):
        return [
            PaperPayload(
                source="web_reach",
                external_id="web:https://example.com/htmx",
                title="HTMX Article Title",
                abstract="Abstract body.",
                authors=["HTMX Author"],
                url=url,
                pdf_url=None,
                published_at=None,
                categories=["web"],
                kind="news",
            )
        ]

    monkeypatch.setattr(
        "app.modules.scrape.sources.external_sources.search_web", mock_search_web
    )

    with client:
        with client.session_transaction() as sess:
            sess["_user_id"] = str(a_user.id)
            sess["_fresh"] = True

        resp = client.post(
            "/papers/add-link",
            data={"url": "https://example.com/htmx"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "HTMX Article Title" in resp.get_data(as_text=True)
        assert "Link" in resp.get_data(as_text=True)
