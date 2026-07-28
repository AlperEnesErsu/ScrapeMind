"""Source adapters — parsing + registry, all with canned responses (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from app.modules.scrape.net_guard import BLOCKED_MESSAGE, is_public_http_url
from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.sources import AVAILABLE_SOURCES, SOURCE_META, enabled_sources, rss_source
from app.modules.scrape.sources import arxiv_source as ax
from app.modules.scrape.sources import pubmed_source as pm
from app.modules.scrape.sources import semantic_scholar_source as ss

_FEED_KEYS = {f["key"] for f in rss_source.FEEDS}

# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------


def test_registry_has_academic_adapters_and_feeds():
    assert {"arxiv", "semantic_scholar", "pubmed"} | _FEED_KEYS == set(AVAILABLE_SOURCES)


def test_every_feed_key_has_source_meta():
    for key in _FEED_KEYS:
        assert key in SOURCE_META
        assert SOURCE_META[key]["label"]


def test_enabled_sources_defaults_to_all(monkeypatch):
    monkeypatch.delenv("SCRAPE_SOURCES", raising=False)
    assert set(enabled_sources()) == {"arxiv", "semantic_scholar", "pubmed"} | _FEED_KEYS


def test_enabled_sources_filters_and_ignores_unknown(monkeypatch):
    monkeypatch.setenv("SCRAPE_SOURCES", "arxiv, nope, PubMed")
    assert set(enabled_sources()) == {"arxiv", "pubmed"}


# ----------------------------------------------------------------------------
# Semantic Scholar
# ----------------------------------------------------------------------------

_SS_ITEM = {
    "paperId": "abc123",
    "title": "Attention Is\nAll You Need",
    "abstract": "  We propose the Transformer.  ",
    "authors": [{"name": "Ashish Vaswani"}, {"name": "Noam Shazeer"}, {"name": ""}],
    "url": "https://www.semanticscholar.org/paper/abc123",
    "openAccessPdf": {"url": "https://example.com/paper.pdf"},
    "publicationDate": "2017-06-12",
    "year": 2017,
    "fieldsOfStudy": ["Computer Science"],
}


def _fake_response(json_data=None, content=b"", status=200):
    resp = SimpleNamespace(
        status_code=status,
        content=content,
        json=lambda: json_data,
        raise_for_status=lambda: None,
    )
    return resp


def test_semantic_scholar_parses_payload(monkeypatch):
    monkeypatch.setattr(
        ss.requests, "get", lambda *a, **k: _fake_response(json_data={"data": [_SS_ITEM]})
    )
    out = ss.search("transformers", max_results=5)
    assert len(out) == 1
    p = out[0]
    assert p.source == "semantic_scholar"
    assert p.external_id == "abc123"
    assert p.title == "Attention Is All You Need"  # newline flattened
    assert p.abstract == "We propose the Transformer."
    assert p.authors == ["Ashish Vaswani", "Noam Shazeer"]  # empty name dropped
    assert p.pdf_url == "https://example.com/paper.pdf"
    assert p.published_at.year == 2017 and p.published_at.month == 6
    assert p.categories == ["Computer Science"]


def test_semantic_scholar_date_falls_back_to_year(monkeypatch):
    item = {**_SS_ITEM, "publicationDate": None}
    monkeypatch.setattr(
        ss.requests, "get", lambda *a, **k: _fake_response(json_data={"data": [item]})
    )
    p = ss.search("x", max_results=1)[0]
    assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2017, 1, 1)


def test_semantic_scholar_skips_records_without_title(monkeypatch):
    items = [{**_SS_ITEM, "title": ""}, _SS_ITEM]
    monkeypatch.setattr(
        ss.requests, "get", lambda *a, **k: _fake_response(json_data={"data": items})
    )
    assert len(ss.search("x", max_results=5)) == 1


def test_semantic_scholar_keyword_loop_dedupes_and_survives_failures(monkeypatch):
    calls = []

    def fake_search(query, *, max_results):
        calls.append(query)
        if query == "bad":
            raise requests.ConnectionError("boom")
        return [ss._to_payload(_SS_ITEM)]

    monkeypatch.setattr(ss, "search", fake_search)
    out = ss.search_for_keywords(["good", "bad", "good2"], max_results=10)
    # Same paperId from two healthy keywords → deduped to one payload.
    assert len(out) == 1
    assert calls == ["good", "bad", "good2"]


def test_semantic_scholar_raises_when_every_keyword_failed(monkeypatch):
    """A wholly-throttled source must not look like "no papers on your topic".

    The unauthenticated endpoint shares a ~100 req / 5 min pool per IP, so 429
    is the normal outcome of two scans in a row. Swallowing it recorded the
    run as `status="ok", hits=0` — the app telling the user their field is
    empty when it never got to ask. Raising lets the service flag the source
    and mark the run "partial".
    """

    def fake_search(query, *, max_results):  # noqa: ARG001
        raise requests.HTTPError("429 Client Error")

    monkeypatch.setattr(ss, "search", fake_search)
    with pytest.raises(requests.HTTPError):
        ss.search_for_keywords(["a", "b"], max_results=10)


def test_semantic_scholar_partial_success_still_returns(monkeypatch):
    """One keyword throttled while another worked is a partial result, not a
    failure — those payloads are real and must still land."""

    def fake_search(query, *, max_results):  # noqa: ARG001
        if query == "bad":
            raise requests.HTTPError("429 Client Error")
        return [ss._to_payload(_SS_ITEM)]

    monkeypatch.setattr(ss, "search", fake_search)
    assert len(ss.search_for_keywords(["bad", "good"], max_results=10)) == 1


@pytest.mark.parametrize(
    ("module", "slot_name", "query"),
    [
        (ss, "semantic_scholar_slot", "x"),
        (pm, "pubmed_slot", "x"),
        (ax, "arxiv_slot", "all:x"),
    ],
)
def test_denied_rate_limit_slot_raises_instead_of_returning_empty(
    module, slot_name, query, monkeypatch
):
    """Same contract for the deployment-wide Redis buckets: giving up on a
    slot is a skipped request, not an empty result set."""
    monkeypatch.setattr(module, slot_name, lambda: False)
    with pytest.raises(SourceThrottledError):
        module.search(query, max_results=5)


# ----------------------------------------------------------------------------
# PubMed
# ----------------------------------------------------------------------------

_PUBMED_XML = b"""<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">12345678</PMID>
      <Article>
        <Journal><JournalIssue><PubDate><Year>2026</Year><Month>Feb</Month><Day>3</Day></PubDate></JournalIssue></Journal>
        <ArticleTitle>CRISPR screening in <i>E. coli</i> models.</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Background text.</AbstractText>
          <AbstractText Label="RESULTS">Results text.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Curie</LastName><ForeName>Marie</ForeName></Author>
          <Author><CollectiveName>The Consortium</CollectiveName></Author>
        </AuthorList>
      </Article>
      <MeshHeadingList>
        <MeshHeading><DescriptorName>Gene Editing</DescriptorName></MeshHeading>
      </MeshHeadingList>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


def test_pubmed_parses_payload(monkeypatch):
    responses = iter(
        [
            _fake_response(json_data={"esearchresult": {"idlist": ["12345678"]}}),
            _fake_response(content=_PUBMED_XML),
        ]
    )
    monkeypatch.setattr(pm.requests, "get", lambda *a, **k: next(responses))
    out = pm.search("crispr", max_results=5)
    assert len(out) == 1
    p = out[0]
    assert p.source == "pubmed"
    assert p.external_id == "12345678"
    # Inline <i> markup flattened into the title text
    assert p.title == "CRISPR screening in E. coli models."
    assert "Background text." in p.abstract and "Results text." in p.abstract
    assert p.authors == ["Marie Curie", "The Consortium"]
    assert p.url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert p.pdf_url is None
    assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2026, 2, 3)
    assert p.categories == ["Gene Editing"]


def test_pubmed_empty_idlist_short_circuits(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _fake_response(json_data={"esearchresult": {"idlist": []}})

    monkeypatch.setattr(pm.requests, "get", fake_get)
    assert pm.search("nothing", max_results=5) == []
    assert len(calls) == 1  # efetch never called


def test_pubmed_keywords_build_or_query(monkeypatch):
    seen = {}

    def fake_search(query, *, max_results):
        seen["query"] = query
        return []

    monkeypatch.setattr(pm, "search", fake_search)
    pm.search_for_keywords(["gene editing", "crispr"], max_results=5)
    assert seen["query"] == '"gene editing" OR "crispr"'


# ----------------------------------------------------------------------------
# RSS feeds (rss_source) — parsed from a saved fixture, no network
#
# `fetch_feed` no longer hands the URL to feedparser (that path had no timeout);
# it does the HTTP itself and parses the bytes. So the fixture is served through
# a stubbed `requests.get` rather than smuggled in as the "url".
# ----------------------------------------------------------------------------

_RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Fake Blog</title>
<item>
  <title>A Big Announcement</title>
  <link>https://example.test/posts/big-announcement</link>
  <guid>https://example.test/posts/big-announcement</guid>
  <description><![CDATA[<p>We shipped <b>something</b> new.  Extra   spaces too.</p>]]></description>
  <pubDate>Thu, 23 Jul 2026 12:00:00 GMT</pubDate>
</item>
<item>
  <title>Second Post</title>
  <link>https://example.test/posts/second</link>
  <guid>https://example.test/posts/second</guid>
  <pubDate>Wed, 22 Jul 2026 09:30:00 GMT</pubDate>
</item>
</channel></rss>
"""

_TITLELESS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Fake Blog</title>
<item>
  <link>https://example.test/posts/no-title</link>
  <guid>https://example.test/posts/no-title</guid>
</item>
</channel></rss>
"""

_FAKE_FEED = {
    "key": "fake_blog",
    "label": "Fake Blog",
    "url": "https://example.test/feed.xml",
    "icon": "bi-broadcast",
    "desc": "A fake feed for tests",
}


class _FakeResponse:
    """Minimal stand-in for a streamed `requests` response."""

    def __init__(self, body: str = "", *, status_code: int = 200, headers: dict | None = None):
        self._body = body.encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


def _allow(monkeypatch):
    """Neutralise the SSRF guard — CI has no outbound DNS, and these tests are
    about parsing, not about the guard (which has its own tests)."""
    monkeypatch.setattr(rss_source, "is_public_http_url", lambda url, **kw: (True, None))


def _serve(monkeypatch, response, *, capture: dict | None = None):
    """Point `rss_source.requests.get` at a canned response."""
    _allow(monkeypatch)

    def _fake_get(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture.update(kwargs)
        return response

    monkeypatch.setattr(rss_source.requests, "get", _fake_get)


def test_rss_fetch_feed_maps_entries_to_payloads(monkeypatch):
    _serve(monkeypatch, _FakeResponse(_RSS_FIXTURE))
    out = rss_source.fetch_feed(_FAKE_FEED)
    assert len(out) == 2
    p = out[0]
    assert p.source == "fake_blog"
    assert p.external_id == "https://example.test/posts/big-announcement"
    assert p.title == "A Big Announcement"
    # HTML stripped + whitespace collapsed
    assert p.abstract == "We shipped something new. Extra spaces too."
    assert p.url == "https://example.test/posts/big-announcement"
    assert p.pdf_url is None
    assert p.authors == []
    assert p.categories == ["announcement"]
    assert p.kind == "news"
    assert p.published_at is not None
    assert p.published_at.year == 2026 and p.published_at.month == 7 and p.published_at.day == 23


def test_rss_fetch_feed_skips_entries_without_title(monkeypatch):
    _serve(monkeypatch, _FakeResponse(_TITLELESS_FIXTURE))
    assert rss_source.fetch_feed(_FAKE_FEED) == []


def test_rss_fetch_feed_returns_empty_on_parse_failure(monkeypatch):
    _serve(monkeypatch, _FakeResponse(_RSS_FIXTURE))

    def _boom(*a, **k):
        raise OSError("bad parser")

    monkeypatch.setattr(rss_source.feedparser, "parse", _boom)
    assert rss_source.fetch_feed(_FAKE_FEED) == []


def test_rss_fetch_feed_returns_empty_on_network_failure(monkeypatch):
    _allow(monkeypatch)

    def _boom(*a, **k):
        raise rss_source.requests.ConnectionError("network unreachable")

    monkeypatch.setattr(rss_source.requests, "get", _boom)
    assert rss_source.fetch_feed(_FAKE_FEED) == []


def test_rss_fetch_sends_conditional_headers(monkeypatch):
    """Storing + replaying ETag/Last-Modified is what keeps a nightly re-fetch
    of an unchanged feed free."""
    seen: dict = {}
    _serve(monkeypatch, _FakeResponse(_RSS_FIXTURE), capture=seen)
    rss_source.fetch_feed_conditional(
        _FAKE_FEED, etag='W/"abc"', last_modified="Thu, 23 Jul 2026 12:00:00 GMT"
    )
    assert seen["headers"]["If-None-Match"] == 'W/"abc"'
    assert seen["headers"]["If-Modified-Since"] == "Thu, 23 Jul 2026 12:00:00 GMT"
    # A read timeout must always be set — this is the whole point of the rewrite
    assert seen["timeout"][1] > 0
    assert seen["allow_redirects"] is False


def test_rss_fetch_304_is_a_noop(monkeypatch):
    _serve(monkeypatch, _FakeResponse("", status_code=304))
    res = rss_source.fetch_feed_conditional(_FAKE_FEED, etag='W/"abc"')
    assert res.status == "not_modified"
    assert res.payloads == []
    assert res.etag == 'W/"abc"'  # echoed back so the caller keeps it stored


def test_rss_fetch_returns_new_validators(monkeypatch):
    _serve(
        monkeypatch,
        _FakeResponse(
            _RSS_FIXTURE,
            headers={"ETag": 'W/"new"', "Last-Modified": "Fri, 24 Jul 2026 00:00:00 GMT"},
        ),
    )
    res = rss_source.fetch_feed_conditional(_FAKE_FEED)
    assert res.status == "ok"
    assert res.etag == 'W/"new"'
    assert res.last_modified == "Fri, 24 Jul 2026 00:00:00 GMT"


def test_rss_fetch_timeout_is_reported(monkeypatch):
    _allow(monkeypatch)

    def _slow(*a, **k):
        raise rss_source.requests.Timeout("read timed out")

    monkeypatch.setattr(rss_source.requests, "get", _slow)
    res = rss_source.fetch_feed_conditional(_FAKE_FEED)
    assert res.status == "timeout"
    assert res.payloads == []


def test_rss_fetch_aborts_oversized_body(monkeypatch, app):
    """A user-supplied URL could serve hundreds of MB; the read must stop."""
    _serve(monkeypatch, _FakeResponse("x" * 5000))
    monkeypatch.setitem(app.config, "FEED_FETCH_MAX_BYTES", 1000)
    with app.app_context():
        res = rss_source.fetch_feed_conditional(_FAKE_FEED)
    assert res.status == "too_large"
    assert res.payloads == []


def test_rss_fetch_http_error_is_reported(monkeypatch):
    _serve(monkeypatch, _FakeResponse("nope", status_code=503))
    res = rss_source.fetch_feed_conditional(_FAKE_FEED)
    assert res.status == "http_error"
    assert res.http_status == 503


def test_rss_fetch_revalidates_ssrf_on_each_redirect(monkeypatch):
    """A public URL that 302s to link-local must not be followed — the guard
    only ever saw the first address at add time.

    The guard itself is stubbed here (CI has no outbound DNS); what this test
    pins down is that *every hop* goes through it, not just the first.
    """
    hops = []
    checked = []

    def _fake_guard(url, *, allow_private=False):
        checked.append(url)
        return ("169.254." not in url), None

    def _fake_get(url, **kwargs):
        hops.append(url)
        if url == _FAKE_FEED["url"]:
            return _FakeResponse(
                "", status_code=302, headers={"Location": "http://169.254.169.254/"}
            )
        raise AssertionError("must not fetch the redirect target")

    monkeypatch.setattr(rss_source, "is_public_http_url", _fake_guard)
    monkeypatch.setattr(rss_source.requests, "get", _fake_get)
    res = rss_source.fetch_feed_conditional(_FAKE_FEED)
    assert res.status == "blocked"
    assert hops == [_FAKE_FEED["url"]]
    assert checked == [_FAKE_FEED["url"], "http://169.254.169.254/"]


def test_rss_search_for_keywords_is_a_noop():
    """RSS feeds are ingested globally, never via the per-user keyword-search
    flow — this must always return [] regardless of input."""
    assert rss_source.search_for_keywords(["anything"], max_results=25) == []


def test_rss_starter_feeds_are_registered():
    keys = {f["key"] for f in rss_source.FEEDS}
    assert keys == {"openai_blog", "google_ai_blog", "deepmind_blog", "huggingface_blog"}
    for f in rss_source.FEEDS:
        assert f["url"].startswith("https://")


# ----------------------------------------------------------------------------
# SSRF guard — feed URLs are user-supplied and fetched server-side
#
# Literal IPs throughout so nothing here needs DNS.
# ----------------------------------------------------------------------------


def test_guard_rejects_loopback_and_private_and_metadata():
    for url in (
        "http://127.0.0.1:80/feed.xml",
        "http://10.0.0.5/feed.xml",
        "http://192.168.1.1/feed.xml",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/feed.xml",
        "http://[::ffff:127.0.0.1]/feed.xml",
        "http://metadata.google.internal/computeMetadata/v1/",
    ):
        ok, err = is_public_http_url(url)
        assert ok is False, url
        assert err == BLOCKED_MESSAGE


def test_guard_rejects_non_http_schemes_and_odd_ports():
    assert is_public_http_url("file:///etc/passwd")[0] is False
    assert is_public_http_url("gopher://example.test/")[0] is False
    # Redis, Postgres, and friends live on non-web ports — never fetch them
    assert is_public_http_url("http://8.8.8.8:6379/")[0] is False


def test_guard_allows_a_public_address():
    assert is_public_http_url("https://8.8.8.8/feed.xml") == (True, None)


def test_guard_short_circuits_when_private_hosts_allowed():
    """FEED_ALLOW_PRIVATE_HOSTS is the documented dev escape hatch."""
    assert is_public_http_url("http://127.0.0.1:8000/feed.xml", allow_private=True) == (True, None)


def test_guard_rejects_unresolvable_host():
    # .invalid is reserved by RFC 2606 and never resolves
    assert is_public_http_url("https://nope.invalid/feed.xml")[0] is False
