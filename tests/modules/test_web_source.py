"""Non-RSS page discovery (Faz 5.2) — the four-rung ladder.

No network: `fetcher.requests.get` serves canned HTML, the SSRF guard is
neutralised on `fetcher`, and robots.txt is allowed by default (its own
behaviour is covered in test_robots.py).

The ordering assertions matter more than the parsing ones. Each rung is a
weaker guess than the one above it, so a test that only checks "we got items"
would pass just as happily if the ladder silently ran backwards.
"""

from __future__ import annotations

import pytest

from app.modules.scrape import fetcher, robots
from app.modules.scrape.sources import web_source as ws


class _FakeResponse:
    def __init__(self, body: str = "", *, status_code: int = 200, headers: dict | None = None):
        self._body = body.encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _allow_everything(monkeypatch):
    monkeypatch.setattr(fetcher, "is_public_http_url", lambda url, **kw: (True, None))
    monkeypatch.setattr(ws.robots, "is_allowed", lambda url, **kw: (True, None))
    monkeypatch.setattr(ws.robots, "host_slot", lambda url: True)


def _serve(monkeypatch, pages: dict[str, _FakeResponse], *, capture: list | None = None):
    """Map URL -> canned response; anything unmapped 404s."""

    def _fake_get(url, **kwargs):
        if capture is not None:
            capture.append(url)
        return pages.get(url, _FakeResponse("nope", status_code=404))

    monkeypatch.setattr(fetcher.requests, "get", _fake_get)


_PAGE = "https://lab.example.test/news"

_FEED_XML = """<?xml version="1.0"?><rss version="2.0"><channel>
<title>Lab News</title>
<item><title>A Feed Item Title</title><link>https://lab.example.test/a</link>
<guid>https://lab.example.test/a</guid><pubDate>Thu, 23 Jul 2026 12:00:00 GMT</pubDate></item>
</channel></rss>"""

_JSONLD_HTML = """<html><head><title>Lab News</title>
<script type="application/ld+json">
{"@type":"ItemList","itemListElement":[
 {"item":{"@type":"BlogPosting","url":"/posts/one","headline":"A JSON-LD Headline",
          "description":"Structured summary.","datePublished":"2026-07-20T09:00:00Z"}},
 {"item":{"@type":"BlogPosting","url":"/posts/two","headline":"Another JSON-LD Headline"}}
]}
</script></head><body><p>hi</p></body></html>"""

_BLOCKS_HTML = """<html><head><title>Lab News</title></head><body>
<nav><a href="/x">Home</a><a href="/y">About</a><a href="/z">Contact</a></nav>
<div class="list">
  <article class="card"><h2>First Announcement Headline</h2>
    <a href="/posts/1">read</a><time datetime="2026-07-18">18 July</time>
    <p>Summary of the first post.</p></article>
  <article class="card"><h2>Second Announcement Headline</h2>
    <a href="/posts/2">read</a><p>Summary of the second post.</p></article>
  <article class="card"><h2>Third Announcement Headline</h2>
    <a href="/posts/3">read</a></article>
</div></body></html>"""

_ARTICLE_HTML = """<html><head><title>One Long Essay</title></head><body>
<article><h1>One Long Essay</h1>
<p>%s</p></article></body></html>""" % (
    "This is a reasonably long paragraph of prose that trafilatura should treat "
    "as the body of a single article rather than a listing of many items. " * 6
)


# ----------------------------------------------------------------------------
# Rung 1 — RSS autodiscovery
# ----------------------------------------------------------------------------


def test_autodiscovered_feed_wins(monkeypatch):
    """A publisher-maintained feed beats anything inferred from markup, so an
    advertised feed must be used even though the page also has JSON-LD."""
    html = _JSONLD_HTML.replace(
        "<title>Lab News</title>",
        '<title>Lab News</title><link rel="alternate" type="application/rss+xml" href="/feed.xml">',
    )
    _serve(
        monkeypatch,
        {
            _PAGE: _FakeResponse(html),
            "https://lab.example.test/feed.xml": _FakeResponse(_FEED_XML),
        },
    )
    res = ws.discover(_PAGE)
    assert res.status == "ok"
    assert res.mode == "rss"
    assert res.feed_url == "https://lab.example.test/feed.xml"
    assert [p.title for p in res.payloads] == ["A Feed Item Title"]


def test_unusable_advertised_feed_falls_through_to_markup(monkeypatch):
    """A site can advertise a feed that 404s or does not parse. That is a
    reason to keep going, not to report the page as empty."""
    html = _JSONLD_HTML.replace(
        "<title>Lab News</title>",
        '<title>Lab News</title><link rel="alternate" type="application/rss+xml" href="/dead.xml">',
    )
    _serve(monkeypatch, {_PAGE: _FakeResponse(html)})  # /dead.xml 404s
    res = ws.discover(_PAGE)
    assert res.status == "ok"
    assert res.mode == "jsonld"


# ----------------------------------------------------------------------------
# Rung 2 — JSON-LD
# ----------------------------------------------------------------------------


def test_jsonld_itemlist_is_read(monkeypatch):
    _serve(monkeypatch, {_PAGE: _FakeResponse(_JSONLD_HTML)})
    res = ws.discover(_PAGE)
    assert res.mode == "jsonld"
    assert [p.title for p in res.payloads] == ["A JSON-LD Headline", "Another JSON-LD Headline"]
    first = res.payloads[0]
    assert first.url == "https://lab.example.test/posts/one"  # relative href absolutised
    assert first.abstract == "Structured summary."
    assert first.published_at is not None and first.published_at.year == 2026
    assert first.kind == "news"
    assert first.source == "user_page"


def test_broken_jsonld_blob_does_not_abort_discovery(monkeypatch):
    html = _BLOCKS_HTML.replace(
        "</head>", '<script type="application/ld+json">{not valid json</script></head>'
    )
    _serve(monkeypatch, {_PAGE: _FakeResponse(html)})
    res = ws.discover(_PAGE)
    assert res.status == "ok"
    assert res.mode == "blocks"  # fell through the broken blob to the next rung


# ----------------------------------------------------------------------------
# Rung 3 — repeated blocks
# ----------------------------------------------------------------------------


def test_repeated_blocks_beat_the_nav_bar(monkeypatch):
    """The nav has more links than the listing has posts. Picking the group
    with the most *usable items* rather than the most nodes is what keeps a
    footer from winning."""
    _serve(monkeypatch, {_PAGE: _FakeResponse(_BLOCKS_HTML)})
    res = ws.discover(_PAGE)
    assert res.mode == "blocks"
    assert [p.title for p in res.payloads] == [
        "First Announcement Headline",
        "Second Announcement Headline",
        "Third Announcement Headline",
    ]
    assert res.payloads[0].url == "https://lab.example.test/posts/1"
    assert res.payloads[0].published_at is not None
    assert res.payloads[0].abstract == "Summary of the first post."
    assert res.payloads[2].abstract is None  # no <p> in the third card


def test_user_selector_overrides_the_heuristic(monkeypatch):
    _serve(monkeypatch, {_PAGE: _FakeResponse(_BLOCKS_HTML)})
    res = ws.discover(_PAGE, mode="blocks", selector="article.card")
    assert len(res.payloads) == 3


def test_invalid_selector_is_user_error_not_a_crash(monkeypatch):
    _serve(monkeypatch, {_PAGE: _FakeResponse(_BLOCKS_HTML)})
    res = ws.discover(_PAGE, mode="blocks", selector="!!! not a selector")
    assert res.status == "empty"


def test_short_link_text_is_not_a_title(monkeypatch):
    """ "More", ">>" and icon links are the usual repeated siblings on a page
    that has no listing at all."""
    html = """<html><body><div>
      <div class="i"><a href="/1">More</a></div>
      <div class="i"><a href="/2">More</a></div>
      <div class="i"><a href="/3">More</a></div>
    </div></body></html>"""
    _serve(monkeypatch, {_PAGE: _FakeResponse(html)})
    res = ws.discover(_PAGE)
    assert res.mode != "blocks"


# ----------------------------------------------------------------------------
# Rung 4 + the empty case
# ----------------------------------------------------------------------------


def test_single_article_falls_to_trafilatura(monkeypatch):
    _serve(monkeypatch, {_PAGE: _FakeResponse(_ARTICLE_HTML)})
    res = ws.discover(_PAGE)
    assert res.mode == "article"
    assert len(res.payloads) == 1
    assert res.payloads[0].title == "One Long Essay"
    assert "trafilatura should treat" in res.payloads[0].abstract


def test_empty_page_reports_empty_with_advice(monkeypatch):
    _serve(monkeypatch, {_PAGE: _FakeResponse("<html><head></head><body></body></html>")})
    res = ws.discover(_PAGE)
    assert res.status == "empty"
    assert res.payloads == []
    assert "CSS selector" in res.detail


def test_pinned_mode_does_not_silently_downgrade(monkeypatch):
    """A page pinned to jsonld that stops emitting it reports empty rather
    than quietly switching to the guessing heuristic and returning different
    items than it did yesterday."""
    _serve(monkeypatch, {_PAGE: _FakeResponse(_BLOCKS_HTML)})
    res = ws.discover(_PAGE, mode="jsonld")
    assert res.status == "empty"


# ----------------------------------------------------------------------------
# Guards
# ----------------------------------------------------------------------------


def test_robots_denial_short_circuits_before_any_fetch(monkeypatch):
    monkeypatch.setattr(
        ws.robots, "is_allowed", lambda url, **kw: (False, robots.DISALLOWED_MESSAGE)
    )

    def _boom(*a, **k):
        raise AssertionError("must not fetch a disallowed page")

    monkeypatch.setattr(fetcher.requests, "get", _boom)
    res = ws.discover(_PAGE)
    assert res.status == "robots_denied"
    assert res.detail == robots.DISALLOWED_MESSAGE


def test_not_modified_short_circuits(monkeypatch):
    _serve(monkeypatch, {_PAGE: _FakeResponse("", status_code=304)})
    res = ws.discover(_PAGE, etag='W/"v1"')
    assert res.status == "not_modified"
    assert res.payloads == []
    assert res.etag == 'W/"v1"'  # echoed back, not nulled


def test_conditional_headers_are_sent(monkeypatch):
    sent: dict = {}

    def _fake_get(url, **kwargs):
        sent.update(kwargs.get("headers") or {})
        return _FakeResponse(_JSONLD_HTML)

    monkeypatch.setattr(fetcher.requests, "get", _fake_get)
    ws.discover(_PAGE, etag='W/"v1"', last_modified="Tue, 12 Aug 2026 10:00:00 GMT")
    assert sent["If-None-Match"] == 'W/"v1"'
    assert sent["If-Modified-Since"] == "Tue, 12 Aug 2026 10:00:00 GMT"


def test_extracted_text_carries_no_markup(monkeypatch):
    """Nothing from a third-party page may reach a template as markup. Jinja
    autoescape is the second half of this; stripping here is the first."""
    html = """<html><head><title>T</title></head><body><div>
      <article class="c"><h2>Headline <em>with</em> <script>alert(1)</script>markup</h2>
        <a href="/1">x</a><p>Body <b>bold</b> text</p></article>
      <article class="c"><h2>Second Headline Long Enough</h2><a href="/2">x</a></article>
      <article class="c"><h2>Third Headline Long Enough</h2><a href="/3">x</a></article>
    </div></body></html>"""
    _serve(monkeypatch, {_PAGE: _FakeResponse(html)})
    res = ws.discover(_PAGE)
    joined = " ".join((p.title or "") + (p.abstract or "") for p in res.payloads)
    assert "<" not in joined and ">" not in joined
    assert "alert(1)" not in joined
