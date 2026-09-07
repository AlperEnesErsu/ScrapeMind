"""robots.txt compliance (Faz 5.2) — fetch, cache, allow/deny, Crawl-delay.

No network: `fetcher.requests.get` is stubbed the same way the RSS tests do
it, and the SSRF guard is neutralised on `fetcher` (CI has no outbound DNS).

The behaviour under test that is easiest to get wrong: this module fails
**closed**. Every other limiter in the scrape stack fails open, so the tests
below pin the asymmetry rather than leaving it to a reviewer's memory.
"""

from __future__ import annotations

import pytest
import requests

from app.modules.scrape import fetcher, robots


class _FakeResponse:
    def __init__(self, body: str = "", *, status_code: int = 200):
        self._body = body.encode("utf-8")
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_cache_no_guard(monkeypatch):
    """Every test here drives `_fetch_robots` directly.

    The Redis handle is forced to None so the cache never answers (its own
    behaviour is covered in the caching tests, which re-patch it), and the SSRF
    guard is neutralised because these tests are about robots.txt semantics.
    """
    monkeypatch.setattr(fetcher, "is_public_http_url", lambda url, **kw: (True, None))
    monkeypatch.setattr("app.modules.scrape.ratelimit._client", lambda: None)


def _serve(monkeypatch, response, *, capture: list | None = None):
    def _fake_get(url, **kwargs):
        if capture is not None:
            capture.append(url)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(fetcher.requests, "get", _fake_get)


_ALLOWS = "User-agent: *\nDisallow: /admin/\nCrawl-delay: 5\n"
_FORBIDS = "User-agent: *\nDisallow: /\n"


def test_robots_url_is_scheme_host_slash_robots():
    assert robots.robots_url("https://example.test/a/b?c=d#e") == "https://example.test/robots.txt"


def test_allowed_path_passes(monkeypatch):
    _serve(monkeypatch, _FakeResponse(_ALLOWS))
    assert robots.is_allowed("https://example.test/news/1") == (True, None)


def test_disallowed_path_is_refused_with_a_reason(monkeypatch):
    _serve(monkeypatch, _FakeResponse(_ALLOWS))
    allowed, reason = robots.is_allowed("https://example.test/admin/secret")
    assert allowed is False
    assert reason == robots.DISALLOWED_MESSAGE


def test_blanket_disallow_is_honoured(monkeypatch):
    _serve(monkeypatch, _FakeResponse(_FORBIDS))
    assert robots.is_allowed("https://example.test/anything")[0] is False


def test_404_robots_means_no_restrictions(monkeypatch):
    """RFC 9309: a missing robots.txt is permission, not a failure."""
    _serve(monkeypatch, _FakeResponse("Not Found", status_code=404))
    assert robots.is_allowed("https://example.test/news/1") == (True, None)


def test_fetch_is_for_the_robots_url_not_the_page(monkeypatch):
    seen: list = []
    _serve(monkeypatch, _FakeResponse(_ALLOWS), capture=seen)
    robots.is_allowed("https://example.test/deep/page.html")
    assert seen == ["https://example.test/robots.txt"]


# ----------------------------------------------------------------------------
# Failing closed — the asymmetry against the rest of the scrape stack
# ----------------------------------------------------------------------------


def test_server_error_means_disallow(monkeypatch):
    _serve(monkeypatch, _FakeResponse("boom", status_code=503))
    allowed, reason = robots.is_allowed("https://example.test/news/1")
    assert allowed is False
    assert reason == robots.UNREACHABLE_MESSAGE


def test_network_failure_means_disallow(monkeypatch):
    _serve(monkeypatch, requests.ConnectionError("unreachable"))
    assert robots.is_allowed("https://example.test/news/1")[0] is False


def test_ssrf_blocked_host_means_disallow(monkeypatch):
    """A blocked robots.txt must not read as allow-all — the two guards are
    additive, not alternatives."""
    monkeypatch.setattr(fetcher, "is_public_http_url", lambda url, **kw: (False, "nope"))

    def _boom(*a, **k):
        raise AssertionError("must not fetch a blocked address")

    monkeypatch.setattr(fetcher.requests, "get", _boom)
    allowed, reason = robots.is_allowed("http://169.254.169.254/latest/")
    assert allowed is False
    assert reason == robots.UNREACHABLE_MESSAGE


def test_oversized_robots_means_disallow(monkeypatch):
    _serve(monkeypatch, _FakeResponse("x" * (robots.MAX_ROBOTS_BYTES + 1)))
    assert robots.is_allowed("https://example.test/news/1")[0] is False


# ----------------------------------------------------------------------------
# Crawl-delay + the per-host slot
# ----------------------------------------------------------------------------


def test_crawl_delay_is_read(monkeypatch):
    _serve(monkeypatch, _FakeResponse(_ALLOWS))
    assert robots.crawl_delay("https://example.test/news/1") == 5.0


def test_crawl_delay_absent_is_none(monkeypatch):
    _serve(monkeypatch, _FakeResponse("User-agent: *\nDisallow: /admin/\n"))
    assert robots.crawl_delay("https://example.test/news/1") is None


def test_host_slot_tightens_limit_to_crawl_delay(app, monkeypatch):
    """Crawl-delay: 30 means at most 2 requests/minute, well under the
    configured ceiling of 10 — so the site's stricter number is the one used."""
    _serve(monkeypatch, _FakeResponse("User-agent: *\nCrawl-delay: 30\n"))
    seen: dict = {}

    def _fake_slot(bucket, limit, per_seconds, **kw):
        seen.update(bucket=bucket, limit=limit, per=per_seconds)
        return True

    monkeypatch.setattr(robots, "acquire_slot", _fake_slot)
    with app.app_context():
        assert robots.host_slot("https://example.test/news/1") is True
    assert seen == {"bucket": "host:example.test", "limit": 2, "per": 60}


def test_host_slot_never_loosens_the_configured_ceiling(app, monkeypatch):
    """A site asking for 0.1s does not get to talk us into hammering it."""
    _serve(monkeypatch, _FakeResponse("User-agent: *\nCrawl-delay: 0.1\n"))
    seen: dict = {}

    def _fake_slot(bucket, limit, per_seconds, **kw):
        seen.update(limit=limit)
        return True

    monkeypatch.setattr(robots, "acquire_slot", _fake_slot)
    with app.app_context():
        robots.host_slot("https://example.test/news/1")
    assert seen["limit"] == 10  # SCRAPE_RATE_HOST_PER_MIN default, not 600


# ----------------------------------------------------------------------------
# Caching — a cache outage must not read as a denial
# ----------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, *, broken: bool = False):
        self.store: dict[str, str] = {}
        self.broken = broken
        self.writes = 0

    def get(self, key):
        if self.broken:
            raise RuntimeError("redis down")
        return self.store.get(key)

    def setex(self, key, ttl, value):
        if self.broken:
            raise RuntimeError("redis down")
        self.writes += 1
        self.store[key] = value


def test_second_check_is_served_from_cache(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("app.modules.scrape.ratelimit._client", lambda: fake)
    seen: list = []
    _serve(monkeypatch, _FakeResponse(_ALLOWS), capture=seen)

    assert robots.is_allowed("https://example.test/a")[0] is True
    assert robots.is_allowed("https://example.test/b")[0] is True
    assert len(seen) == 1  # one robots.txt fetch for two page checks
    assert fake.writes == 1


def test_cache_outage_degrades_to_a_live_fetch_not_a_denial(monkeypatch):
    fake = _FakeRedis(broken=True)
    monkeypatch.setattr("app.modules.scrape.ratelimit._client", lambda: fake)
    _serve(monkeypatch, _FakeResponse(_ALLOWS))
    assert robots.is_allowed("https://example.test/news/1") == (True, None)
