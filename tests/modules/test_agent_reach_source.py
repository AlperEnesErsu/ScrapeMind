"""Tests for Agent Reach source adapter integration in ScrapeMind."""

from __future__ import annotations

import pytest

from app.modules.scrape.sources import (
    AVAILABLE_SOURCES,
    SOURCE_META,
    agent_reach_source,
    enabled_sources,
    source_options,
)
from app.modules.scrape.sources.payload import PaperPayload


def test_agent_reach_sources_registered():
    """Verify youtube_reach, github_reach, and web_reach are registered correctly."""
    assert "youtube_reach" in AVAILABLE_SOURCES
    assert "github_reach" in AVAILABLE_SOURCES
    assert "web_reach" in AVAILABLE_SOURCES

    assert AVAILABLE_SOURCES["youtube_reach"] == agent_reach_source.youtube_adapter
    assert AVAILABLE_SOURCES["github_reach"] == agent_reach_source.github_adapter
    assert AVAILABLE_SOURCES["web_reach"] == agent_reach_source.web_adapter

    assert "youtube_reach" in SOURCE_META
    assert SOURCE_META["youtube_reach"]["icon"] == "bi-youtube"
    assert "github_reach" in SOURCE_META
    assert SOURCE_META["github_reach"]["icon"] == "bi-github"
    assert "web_reach" in SOURCE_META
    assert SOURCE_META["web_reach"]["icon"] == "bi-globe2"

    enabled = enabled_sources()
    assert "youtube_reach" in enabled
    assert "github_reach" in enabled
    assert "web_reach" in enabled


def test_agent_reach_search_youtube_payload_structure(monkeypatch):
    """Test search_youtube returns PaperPayload objects with correct URLs and metadata."""
    sample_yt_json = (
        '{"id": "dQw4w9WgXcQ", "title": "Test Video", "uploader": "Test Channel", "description": "Test Desc", "duration": 180}\n'
    )

    class DummyCompletedProcess:
        returncode = 0
        stdout = sample_yt_json
        stderr = ""

    def dummy_run(*args, **kwargs):
        return DummyCompletedProcess()

    monkeypatch.setattr("subprocess.run", dummy_run)

    payloads = agent_reach_source.search_youtube("test query")
    assert len(payloads) == 1
    p = payloads[0]

    assert isinstance(p, PaperPayload)
    assert p.source == "youtube_reach"
    assert p.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert p.title == "🎥 Test Video"
    assert p.authors == ["Test Channel"]
    assert p.kind == "video"


def test_agent_reach_search_github_payload_structure(monkeypatch):
    """Test search_github returns PaperPayload objects with correct repo URLs."""
    sample_gh_json = (
        '[{"fullName": "test/repo", "url": "https://github.com/test/repo", "description": "Repo desc", "owner": {"login": "testowner"}}]'
    )

    class DummyCompletedProcess:
        returncode = 0
        stdout = sample_gh_json
        stderr = ""

    def dummy_run(*args, **kwargs):
        return DummyCompletedProcess()

    monkeypatch.setattr("subprocess.run", dummy_run)

    payloads = agent_reach_source.search_github("test query")
    assert len(payloads) == 1
    p = payloads[0]

    assert isinstance(p, PaperPayload)
    assert p.source == "github_reach"
    assert p.url == "https://github.com/test/repo"
    assert p.title == "📦 test/repo"
    assert p.authors == ["testowner"]
    assert p.kind == "github"


def test_agent_reach_channel_adapters_dispatch(monkeypatch):
    """Verify that calling search_for_keywords on YouTube adapter calls search_youtube, etc."""
    yt_called = False
    gh_called = False
    web_called = False

    def mock_yt(query, max_results=5):
        nonlocal yt_called
        yt_called = True
        return []

    def mock_gh(query, max_results=5):
        nonlocal gh_called
        gh_called = True
        return []

    def mock_web(query, max_results=10):
        nonlocal web_called
        web_called = True
        return []

    monkeypatch.setattr(agent_reach_source, "search_youtube", mock_yt)
    monkeypatch.setattr(agent_reach_source, "search_github", mock_gh)
    monkeypatch.setattr(agent_reach_source, "search_web", mock_web)

    agent_reach_source.youtube_adapter.search_for_keywords(["test"])
    assert yt_called and not gh_called and not web_called

    yt_called = False
    agent_reach_source.github_adapter.search_for_keywords(["test"])
    assert gh_called and not yt_called and not web_called

    gh_called = False
    agent_reach_source.web_adapter.search_for_keywords(["https://example.com"])
    assert web_called and not yt_called and not gh_called
