"""Tests for external source adapters (YouTube, GitHub, Web Reader) in ScrapeMind."""

from __future__ import annotations

import pytest

from app.modules.scrape.sources import (
    AVAILABLE_SOURCES,
    SOURCE_META,
    enabled_sources,
    external_sources,
    source_options,
)
from app.modules.scrape.sources.payload import PaperPayload


def test_external_sources_registered():
    """Verify youtube_reach, github_reach, and web_reach are registered correctly."""
    assert "youtube_reach" in AVAILABLE_SOURCES
    assert "github_reach" in AVAILABLE_SOURCES
    assert "web_reach" in AVAILABLE_SOURCES

    assert AVAILABLE_SOURCES["youtube_reach"] == external_sources.youtube_adapter
    assert AVAILABLE_SOURCES["github_reach"] == external_sources.github_adapter
    assert AVAILABLE_SOURCES["web_reach"] == external_sources.web_adapter

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
        '{"id": "dQw4w9WgXcQ", "title": "Test Video", "uploader": "Test Channel", "description": "Test Desc", "duration": 180, "upload_date": "20240512"}\n'
    )

    captured_cmd = []

    class DummyCompletedProcess:
        returncode = 0
        stdout = sample_yt_json
        stderr = ""

    def dummy_run(cmd, *args, **kwargs):
        captured_cmd.extend(cmd)
        return DummyCompletedProcess()

    monkeypatch.setattr("subprocess.run", dummy_run)

    payloads = external_sources.search_youtube("test query")
    assert len(payloads) == 1
    p = payloads[0]

    assert isinstance(p, PaperPayload)
    assert p.source == "youtube_reach"
    assert p.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert p.title == "Test Video"
    assert p.authors == ["Test Channel"]
    assert p.kind == "video"
    assert p.published_at is not None
    assert p.published_at.year == 2024
    assert p.published_at.month == 5
    assert p.published_at.day == 12

    # Verify --flat-playlist is NOT in cmd so full metadata (upload_date) is extracted
    assert "--flat-playlist" not in captured_cmd


def test_agent_reach_search_github_payload_structure(monkeypatch):
    """Test search_github returns PaperPayload objects with correct repo URLs."""
    sample_gh_json = (
        '[{"fullName": "test/repo", "url": "https://github.com/test/repo", "description": "Repo desc", "owner": {"login": "testowner"}, "updatedAt": "2024-06-15T10:00:00Z"}]'
    )

    class DummyCompletedProcess:
        returncode = 0
        stdout = sample_gh_json
        stderr = ""

    def dummy_run(*args, **kwargs):
        return DummyCompletedProcess()

    monkeypatch.setattr("subprocess.run", dummy_run)

    payloads = external_sources.search_github("test query")
    assert len(payloads) == 1
    p = payloads[0]

    assert isinstance(p, PaperPayload)
    assert p.source == "github_reach"
    assert p.url == "https://github.com/test/repo"
    assert p.title == "test/repo"
    assert p.authors == ["testowner"]
    assert p.kind == "github"
    assert p.published_at is not None
    assert p.published_at.year == 2024
    assert p.published_at.month == 6
    assert p.published_at.day == 15


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

    monkeypatch.setattr(external_sources, "search_youtube", mock_yt)
    monkeypatch.setattr(external_sources, "search_github", mock_gh)
    monkeypatch.setattr(external_sources, "search_web", mock_web)

    external_sources.youtube_adapter.search_for_keywords(["test"])
    assert yt_called and not gh_called and not web_called

    yt_called = False
    external_sources.github_adapter.search_for_keywords(["test"])
    assert gh_called and not yt_called and not web_called

    gh_called = False
    external_sources.web_adapter.search_for_keywords(["https://example.com"])
    assert web_called and not yt_called and not gh_called


def test_agent_reach_search_github_missing_cli(monkeypatch):
    """Test search_github handles missing gh CLI (FileNotFoundError) gracefully."""

    def dummy_run(*args, **kwargs):
        raise FileNotFoundError("gh binary not found")

    monkeypatch.setattr("subprocess.run", dummy_run)

    payloads = external_sources.search_github("test query")
    assert payloads == []


def test_agent_reach_search_web_net_guard_blocking(monkeypatch):
    """Test search_web blocks SSRF targets like 127.0.0.1 via net_guard."""
    payloads = external_sources.search_web("http://127.0.0.1:6379/")
    assert payloads == []


def test_agent_reach_search_web_jina_reader_success(monkeypatch):
    """Test search_web uses Jina Reader (r.jina.ai) for direct URLs."""

    class DummyResponse:
        status_code = 200
        text = "Title: Sample Web Page\n\nThis is markdown text from Jina Reader."

    def dummy_get(url, headers=None, timeout=15):
        assert "r.jina.ai" in url
        return DummyResponse()

    monkeypatch.setattr("requests.get", dummy_get)

    payloads = external_sources.search_web("https://example.com/test")
    assert len(payloads) == 1
    p = payloads[0]
    assert isinstance(p, PaperPayload)
    assert p.source == "web_reach"
    assert p.title == "Sample Web Page"
    assert "Jina Reader" in p.abstract


def test_agent_reach_search_web_rss_fallback(monkeypatch):
    """Test keyword web search falls back to Google News RSS search."""
    sample_rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
        <channel>
            <title>Google News</title>
            <item>
                <title>Python 3.14 Released</title>
                <link>https://news.example.com/python314</link>
                <guid>https://news.example.com/python314</guid>
                <pubDate>Mon, 03 Aug 2026 12:00:00 GMT</pubDate>
                <description>&lt;p&gt;Python 3.14 brings new speed &amp;amp; Features!&lt;/p&gt;</description>
            </item>
        </channel>
    </rss>"""

    class DummyResponse:
        status_code = 200
        content = sample_rss_xml.encode("utf-8")

    def dummy_get(url, headers=None, timeout=15):
        return DummyResponse()

    monkeypatch.setattr("requests.get", dummy_get)

    payloads = external_sources.search_web("python release")
    assert len(payloads) == 1
    p = payloads[0]
    assert isinstance(p, PaperPayload)
    assert p.source == "web_reach"
    assert p.title == "Python 3.14 Released"
    assert p.url == "https://news.example.com/python314"
    assert "Python 3.14 brings new speed & Features!" in p.abstract
    assert "<p>" not in p.abstract  # Verifies HTML tag stripping
    assert p.published_at is not None
    assert p.published_at.year == 2026

