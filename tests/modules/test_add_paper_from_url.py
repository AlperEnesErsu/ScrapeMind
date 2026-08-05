"""Tests for manually adding papers from URLs (add_paper_from_url)."""

from __future__ import annotations

import pytest
from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.modules.scrape.models import UserPaper
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

    monkeypatch.setattr("app.modules.scrape.sources.external_sources.search_web", mock_search_web)

    link, created = add_paper_from_url(a_user, "https://example.com/article")
    assert created is True
    assert isinstance(link, UserPaper)
    assert link.paper.source == "manual"
    assert link.paper.kind == "link"
    assert link.paper.title == "Sample Article Title"
    assert link.paper.url == "https://example.com/article"
    # No stored label: a translated string here would freeze the row into the
    # language that happened to be active. The card labels manual rows off
    # `paper.source` instead, so every language renders correctly.
    assert link.matched_keyword is None
    # AI is disabled in tests, so the cleaned reader text survives untouched.
    assert link.paper.abstract == "Clean plain text abstract of the article."


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

    monkeypatch.setattr("app.modules.scrape.sources.external_sources.search_web", mock_search_web)

    link1, created1 = add_paper_from_url(a_user, "https://example.com/dedup")
    assert created1 is True

    link2, created2 = add_paper_from_url(a_user, "https://example.com/dedup")
    assert created2 is False
    assert link1.id == link2.id


def test_add_paper_from_url_unreachable(db, a_user, monkeypatch):
    """Verify add_paper_from_url raises ValueError if content fetch fails."""

    def mock_search_web(url):
        return []

    monkeypatch.setattr("app.modules.scrape.sources.external_sources.search_web", mock_search_web)

    with pytest.raises(ValueError):
        add_paper_from_url(a_user, "https://example.com/404-not-found")


def test_add_link_route_htmx(client, a_user, db, monkeypatch):
    """Test POST /papers/add-link route via HTMX."""

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

    monkeypatch.setattr("app.modules.scrape.sources.external_sources.search_web", mock_search_web)

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


def test_add_paper_from_url_uses_llm_summary_when_available(db, a_user, monkeypatch):
    """The LLM summary replaces the cleaned reader text when AI is enabled."""

    def mock_search_web(url):
        return [
            PaperPayload(
                source="web_reach",
                external_id="web:https://example.com/summarised",
                title="Long Page",
                abstract="Language: English | Deutsch\n\nInstall from official sources only.",
                authors=["Web Reader"],
                url=url,
                pdf_url=None,
                published_at=None,
                categories=["web"],
                kind="news",
            )
        ]

    monkeypatch.setattr("app.modules.scrape.sources.external_sources.search_web", mock_search_web)
    monkeypatch.setattr(
        "app.modules.scrape.ai_service.summarize_web_content",
        lambda title, content, user=None: "A concise two sentence summary. It describes the page.",
    )

    link, _created = add_paper_from_url(a_user, "https://example.com/summarised")
    assert link.paper.abstract == "A concise two sentence summary. It describes the page."


def test_add_paper_from_url_survives_a_failing_summariser(db, a_user, monkeypatch):
    """A summariser blowing up must not cost the user the link."""

    def mock_search_web(url):
        return [
            PaperPayload(
                source="web_reach",
                external_id="web:https://example.com/llm-down",
                title="Page",
                abstract="Cleaned fallback text.",
                authors=["Web Reader"],
                url=url,
                pdf_url=None,
                published_at=None,
                categories=["web"],
                kind="news",
            )
        ]

    def boom(title, content, user=None):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr("app.modules.scrape.sources.external_sources.search_web", mock_search_web)
    monkeypatch.setattr("app.modules.scrape.ai_service.summarize_web_content", boom)

    link, created = add_paper_from_url(a_user, "https://example.com/llm-down")
    assert created is True
    assert link.paper.abstract == "Cleaned fallback text."
