"""User-supplied URLs must not reach private addresses through a redirect.

`is_public_http_url` only sees the first address; a public URL that 302s to
the cloud metadata endpoint would slip past it if the fetch followed redirects
on its own. Each path below must go through `fetcher.get_with_redirects`,
which re-validates every hop.
"""

from __future__ import annotations

import pytest
import requests

from app.modules.scrape.sources import external_sources
from app.modules.scrape.sources import youtube_channel_source as yc

PUBLIC = "http://93.184.216.34/page"  # literal public IP: no DNS needed
METADATA = "http://169.254.169.254/latest/meta-data/"


class _Resp:
    def __init__(self, status, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self.text = body.decode()
        self.content = body
        self.encoding = "utf-8"
        self._body = body

    def iter_content(self, chunk_size=1):
        yield self._body

    def close(self):
        pass


@pytest.fixture
def no_private_hosts(app, monkeypatch):
    monkeypatch.setitem(app.config, "FEED_ALLOW_PRIVATE_HOSTS", False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    with app.app_context():
        yield


@pytest.fixture
def redirect_to_metadata(monkeypatch):
    """Every non-Jina GET of the public URL answers 302 -> metadata endpoint."""
    seen: list[str] = []

    def fake_get(url, *args, **kwargs):
        seen.append(url)
        if url.startswith(METADATA):
            return _Resp(200, body=b"iam-credentials")
        if "r.jina.ai" in url:
            return _Resp(500)
        if kwargs.get("allow_redirects", True):
            # What requests itself does by default: follow the redirect.
            return fake_get(METADATA, *args, **kwargs)
        return _Resp(302, {"Location": METADATA})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(external_sources, "web_reach_slot", lambda: True)
    return seen


def test_search_web_direct_fetch_does_not_follow_redirect_to_private(
    no_private_hosts, redirect_to_metadata
):
    assert external_sources.search_web(PUBLIC) == []
    assert METADATA not in redirect_to_metadata
    assert PUBLIC in redirect_to_metadata  # the public URL itself was tried


def test_youtube_channel_resolve_does_not_follow_redirect_to_private(
    no_private_hosts, redirect_to_metadata
):
    channel_id, error = yc._resolve_channel_id_from_page(PUBLIC)
    assert channel_id is None
    assert error
    assert METADATA not in redirect_to_metadata


def test_youtube_caption_fetch_does_not_follow_redirect_to_private(
    no_private_hosts, redirect_to_metadata
):
    assert yc._fetch_caption_body(PUBLIC) is None
    assert METADATA not in redirect_to_metadata
