"""Open-access full text: the licence gate, and the fetch path around it.

The gate is the part worth the most tests. Getting extraction wrong loses a
paper; getting the licence wrong republishes someone else's work.
"""

from __future__ import annotations

import pytest

from app.modules.scrape import fulltext

# --------------------------------------------------------------------------
# The licence gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "license_",
    [
        "cc-by",
        "cc-by-sa",
        "cc-by-nc",
        "cc-by-nd",
        "cc-by-nc-sa",
        "cc-by-nc-nd",
        "cc0",
        "public-domain",
    ],
)
def test_creative_commons_and_public_domain_may_be_stored(license_):
    """Every CC variant permits redistributing the unmodified work.

    NC and ND restrict commercial use and derivative works; neither restricts
    keeping and searching a copy, which is all this gate controls.
    """
    assert fulltext.may_store(license_) is True


@pytest.mark.parametrize(
    "license_",
    [
        None,
        "",
        # Free to read on the publisher's site, no licence at all. The exact
        # case the gate exists for.
        "publisher-specific-oa",
        "other-oa",
        "all-rights-reserved",
    ],
)
def test_readable_but_not_redistributable_may_not_be_stored(license_):
    assert fulltext.may_store(license_) is False


def test_licence_matching_ignores_case_and_padding():
    """OpenAlex is consistent, but a value arriving from an enrichment path or
    a fixture should not slip through the gate on whitespace."""
    assert fulltext.may_store("  CC-BY  ") is True
    assert fulltext.may_store("CC-BY-NC-ND") is True


def test_unknown_licence_defaults_to_not_storable():
    """The default has to be "no".

    A licence this project has never heard of is not evidence of permission,
    and a gate that fails open is not a gate.
    """
    assert fulltext.may_store("some-new-licence-2031") is False


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def test_html_extraction_drops_navigation_chrome():
    html = b"""<html><body>
      <nav><a href="/">Home</a><a href="/about">About</a></nav>
      <article><p>%s</p></article>
      <footer>Copyright notice</footer>
    </body></html>""" % (b"Serious findings about pomegranates. " * 40)
    text = fulltext._extract_html(html)
    assert "pomegranates" in text
    assert "About" not in text


def test_normalise_collapses_the_whitespace_pdfs_produce():
    messy = "one   two\t\tthree\n\n\n\n\nfour   "
    assert fulltext._normalise(messy) == "one two three\n\nfour"


def test_unreadable_pdf_is_an_error_not_a_crash():
    with pytest.raises(fulltext.FullTextError, match="unreadable PDF"):
        fulltext._extract_pdf(b"%PDF-1.4 truncated garbage")


# --------------------------------------------------------------------------
# The fetch path
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, *, status=200, content_type="text/html", body=b""):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        self.closed = True


@pytest.fixture
def allow_everything(monkeypatch):
    monkeypatch.setattr(fulltext.robots, "is_allowed", lambda url, **kw: (True, None))
    monkeypatch.setattr(fulltext.robots, "host_slot", lambda url: True)
    monkeypatch.setattr(fulltext, "fetch_budget", lambda: ((5, 10), 1024 * 1024, True))


def test_robots_denial_stops_the_fetch(monkeypatch):
    """§11 makes robots.txt a contract, so this must refuse before any GET.

    Asserted by leaving get_with_redirects un-mocked: if the guard let it
    through, the test would attempt a real network call.
    """
    monkeypatch.setattr(fulltext.robots, "is_allowed", lambda url, **kw: (False, "Disallow: /"))
    called = []
    monkeypatch.setattr(
        fulltext, "get_with_redirects", lambda *a, **kw: called.append(a) or (None, "", "ok")
    )

    with pytest.raises(fulltext.FullTextError, match="robots.txt"):
        fulltext.fetch_fulltext("https://example.test/paper.pdf", license_="cc-by")
    assert called == [], "a robots denial must not reach the network"


def test_host_rate_limit_stops_the_fetch(monkeypatch):
    monkeypatch.setattr(fulltext.robots, "is_allowed", lambda url, **kw: (True, None))
    monkeypatch.setattr(fulltext.robots, "host_slot", lambda url: False)
    with pytest.raises(fulltext.FullTextError, match="rate limit"):
        fulltext.fetch_fulltext("https://example.test/paper.pdf", license_="cc-by")


def test_blocked_redirect_is_reported(monkeypatch, allow_everything):
    monkeypatch.setattr(fulltext, "get_with_redirects", lambda *a, **kw: (None, "", "blocked"))
    with pytest.raises(fulltext.FullTextError, match="blocked"):
        fulltext.fetch_fulltext("https://example.test/paper.pdf", license_="cc-by")


def test_http_error_is_reported_and_response_closed(monkeypatch, allow_everything):
    resp = _Resp(status=403)
    monkeypatch.setattr(fulltext, "get_with_redirects", lambda *a, **kw: (resp, "", "ok"))
    with pytest.raises(fulltext.FullTextError, match="HTTP 403"):
        fulltext.fetch_fulltext("https://example.test/paper.pdf", license_="cc-by")
    assert resp.closed, "the streaming response must be closed even on an error path"


def test_unsupported_content_type_is_refused(monkeypatch, allow_everything):
    resp = _Resp(content_type="application/zip", body=b"PK\x03\x04rest")
    monkeypatch.setattr(fulltext, "get_with_redirects", lambda *a, **kw: (resp, "", "ok"))
    with pytest.raises(fulltext.FullTextError, match="unsupported content type"):
        fulltext.fetch_fulltext("https://example.test/data.zip", license_="cc-by")


def test_pdf_is_recognised_by_signature_not_only_by_header(monkeypatch, allow_everything):
    """OA repositories serve PDFs as octet-stream often enough that trusting
    the header alone loses them."""
    body = b"%PDF-1.4 " + b"x" * 200
    resp = _Resp(content_type="application/octet-stream", body=body)
    monkeypatch.setattr(fulltext, "get_with_redirects", lambda *a, **kw: (resp, "", "ok"))
    seen = {}

    def _record(payload):
        seen["payload"] = payload
        return "y" * 900

    monkeypatch.setattr(fulltext, "_extract_pdf", _record)
    result = fulltext.fetch_fulltext("https://example.test/x", license_="cc-by")
    assert seen["payload"] == body
    assert result.chars == 900


def test_too_little_text_is_a_failure_not_a_short_paper(monkeypatch, allow_everything):
    """A scanned PDF extracts a few dozen characters of metadata. Recording
    that as a successful fetch would misreport it forever."""
    resp = _Resp(content_type="application/pdf", body=b"%PDF-1.4")
    monkeypatch.setattr(fulltext, "get_with_redirects", lambda *a, **kw: (resp, "", "ok"))
    monkeypatch.setattr(fulltext, "_extract_pdf", lambda payload: "short")
    with pytest.raises(fulltext.FullTextError, match="only 5 characters"):
        fulltext.fetch_fulltext("https://example.test/scan.pdf", license_="cc-by")


def test_result_carries_the_licence_decision(monkeypatch, allow_everything):
    """The caller is handed `storable` rather than the licence string, so a
    call site cannot forget to check it."""
    resp = _Resp(content_type="text/html", body=b"<html></html>")
    monkeypatch.setattr(fulltext, "get_with_redirects", lambda *a, **kw: (resp, "", "ok"))
    monkeypatch.setattr(fulltext, "_extract_html", lambda payload: "z" * 900)

    open_ = fulltext.fetch_fulltext("https://example.test/a", license_="cc-by")
    assert open_.storable is True

    bronze = fulltext.fetch_fulltext("https://example.test/b", license_="publisher-specific-oa")
    assert bronze.storable is False
    assert bronze.chars == 900, "text is still returned; only storing it is refused"
