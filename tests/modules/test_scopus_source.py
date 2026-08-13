"""Scopus adapter + the discovery-only guarantee (Faz 5.4).

The load-bearing assertions here are licence constraints, not preferences:
no Elsevier abstract is ever persisted, and the source cannot run without both
a key and an admin opt-in. See docs/adr/0002-elsevier-discovery-only.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.sources import SOURCE_META
from app.modules.scrape.sources import scopus_source as sc

_ENTRY = {
    "dc:title": "A Study of\nSomething",
    "prism:doi": "10.1234/abc",
    "prism:coverDate": "2024-07-01",
    "dc:description": "AN ELSEVIER ABSTRACT THAT MUST NEVER BE STORED",
    "link": [
        {"@ref": "self", "@href": "https://api.elsevier.com/x"},
        {"@ref": "scopus", "@href": "https://www.scopus.com/record/display.uri?eid=2-s2.0-1"},
    ],
}


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def _results(entries):
    return {"search-results": {"entry": entries}}


@pytest.fixture(autouse=True)
def creds_and_no_throttle(monkeypatch):
    monkeypatch.setenv("SCOPUS_API_KEY", "test-key")
    monkeypatch.delenv("SCOPUS_INSTTOKEN", raising=False)
    monkeypatch.setattr(sc, "scopus_slot", lambda: True)
    monkeypatch.setattr(sc, "consume_quota", lambda *a, **k: True)


@pytest.fixture
def one_result(monkeypatch):
    monkeypatch.setattr(sc.requests, "get", lambda *a, **k: _Resp(_results([_ENTRY])))
    return sc.search_for_keywords(["robotics"])[0]


# ----------------------------------------------------------------------------
# The licence constraint
# ----------------------------------------------------------------------------


def test_abstract_is_never_carried(one_result):
    """ADR-0002: no Elsevier-licensed content is persisted. The response
    contains an abstract; the payload must not."""
    assert one_result.abstract is None


def test_no_field_leaks_the_abstract(one_result):
    """Belt and braces — the text must not reappear anywhere in the payload."""
    serialized = str(one_result.as_dict())
    assert "MUST NEVER BE STORED" not in serialized


def test_url_links_back_to_scopus(one_result):
    """Linking out is what the licence allows in place of reproducing
    content."""
    assert "scopus.com" in one_result.url


def test_record_without_a_doi_is_skipped(monkeypatch):
    """The DOI is the whole hydration mechanism. Without one there is no way
    to fetch storable metadata, so keeping the row would mean persisting
    Elsevier's title with no way to improve it."""
    no_doi = {k: v for k, v in _ENTRY.items() if k != "prism:doi"}
    monkeypatch.setattr(sc.requests, "get", lambda *a, **k: _Resp(_results([no_doi, _ENTRY])))

    out = sc.search_for_keywords(["x"])
    assert len(out) == 1
    assert out[0].doi == "10.1234/abc"


# ----------------------------------------------------------------------------
# Gating
# ----------------------------------------------------------------------------


def test_registered_with_both_gates():
    meta = SOURCE_META["scopus"]
    assert meta["requires_key"] is True
    assert meta["requires_admin_optin"] == "scopus_enabled"


def test_missing_key_raises_rather_than_returning_empty(monkeypatch):
    monkeypatch.delenv("SCOPUS_API_KEY", raising=False)
    with pytest.raises(SourceThrottledError):
        sc.search_for_keywords(["x"])


def test_key_is_sent_as_a_header(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs["headers"])
        return _Resp(_results([]))

    monkeypatch.setattr(sc.requests, "get", fake_get)
    sc.search_for_keywords(["x"])

    assert seen["X-ELS-APIKey"] == "test-key"
    assert "X-ELS-Insttoken" not in seen


def test_insttoken_is_sent_when_configured(monkeypatch):
    """Needed off campus — the key alone is bound to an institution's IP."""
    seen = {}
    monkeypatch.setenv("SCOPUS_INSTTOKEN", "tok")

    def fake_get(url, **kwargs):
        seen.update(kwargs["headers"])
        return _Resp(_results([]))

    monkeypatch.setattr(sc.requests, "get", fake_get)
    sc.search_for_keywords(["x"])
    assert seen["X-ELS-Insttoken"] == "tok"


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_request_raises_throttled(monkeypatch, status):
    """ "Valid key, wrong network" is the confusing failure here, so it is
    surfaced as a throttle rather than a generic HTTP error."""
    monkeypatch.setattr(sc.requests, "get", lambda *a, **k: _Resp(status_code=status))
    with pytest.raises(SourceThrottledError):
        sc.search_for_keywords(["x"])


def test_exhausted_quota_raises_before_calling(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not have called Scopus with no budget")

    monkeypatch.setattr(sc, "consume_quota", lambda *a, **k: False)
    monkeypatch.setattr(sc.requests, "get", boom)

    with pytest.raises(SourceThrottledError):
        sc.search_for_keywords(["x"])


# ----------------------------------------------------------------------------
# Query + parsing
# ----------------------------------------------------------------------------


def test_query_ors_the_whole_keyword_set():
    assert sc.build_query(["robotics", "cnc"]) == 'TITLE-ABS-KEY("robotics" OR "cnc")'


def test_query_strips_quotes():
    assert sc.build_query(['say "hi"']) == 'TITLE-ABS-KEY("say hi")'


def test_blank_keywords_make_no_request(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not have called Scopus")

    monkeypatch.setattr(sc.requests, "get", boom)
    assert sc.search_for_keywords(["  "]) == []


def test_title_and_date_are_parsed(one_result):
    assert one_result.title == "A Study of Something"
    assert one_result.published_at == datetime(2024, 7, 1, tzinfo=UTC)


def test_server_error_propagates(monkeypatch):
    monkeypatch.setattr(sc.requests, "get", lambda *a, **k: _Resp(status_code=500))
    with pytest.raises(requests.HTTPError):
        sc.search_for_keywords(["x"])


# ----------------------------------------------------------------------------
# Hydration — the working half of the discovery-only design
# ----------------------------------------------------------------------------


def test_hydration_replaces_the_payload_with_openalex(app, monkeypatch):
    from app.modules.scrape import service
    from app.modules.scrape.sources import openalex_source as oa
    from app.modules.scrape.sources.payload import PaperPayload

    scopus_payload = PaperPayload(
        source="scopus",
        external_id="10.1234/abc",
        title="Scopus title",
        abstract=None,
        authors=[],
        url="https://www.scopus.com/x",
        pdf_url=None,
        published_at=None,
        categories=[],
        doi="10.1234/abc",
    )
    hydrated = PaperPayload(
        source="openalex",
        external_id="W1",
        title="OpenAlex title",
        abstract="A freely licensed abstract.",
        authors=["Jane Smith"],
        url="https://doi.org/10.1234/abc",
        pdf_url=None,
        published_at=None,
        categories=["cs"],
        doi="10.1234/abc",
    )
    monkeypatch.setattr(oa, "fetch_by_doi", lambda doi: hydrated)

    out = service._hydrate_scopus_payloads([scopus_payload])
    assert out[0].source == "openalex"
    assert out[0].abstract == "A freely licensed abstract."


def test_unhydrated_payload_is_kept_as_discovery(app, monkeypatch):
    """A DOI OpenAlex has never heard of keeps its Scopus payload: a title and
    a link, no abstract. That is the residual exposure ADR-0002 names."""
    from app.modules.scrape import service
    from app.modules.scrape.sources import openalex_source as oa
    from app.modules.scrape.sources.payload import PaperPayload

    scopus_payload = PaperPayload(
        source="scopus",
        external_id="10.1234/abc",
        title="Scopus title",
        abstract=None,
        authors=[],
        url="https://www.scopus.com/x",
        pdf_url=None,
        published_at=None,
        categories=[],
        doi="10.1234/abc",
    )
    monkeypatch.setattr(oa, "fetch_by_doi", lambda doi: None)

    out = service._hydrate_scopus_payloads([scopus_payload])
    assert out[0].source == "scopus"
    assert out[0].abstract is None


def test_hydration_failure_keeps_the_result(app, monkeypatch):
    """Losing the result entirely would be worse than showing a bare title."""
    from app.modules.scrape import service
    from app.modules.scrape.sources import openalex_source as oa
    from app.modules.scrape.sources.payload import PaperPayload

    scopus_payload = PaperPayload(
        source="scopus",
        external_id="10.1234/abc",
        title="Scopus title",
        abstract=None,
        authors=[],
        url=None,
        pdf_url=None,
        published_at=None,
        categories=[],
        doi="10.1234/abc",
    )

    def boom(doi):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(oa, "fetch_by_doi", boom)
    out = service._hydrate_scopus_payloads([scopus_payload])
    assert len(out) == 1
    assert out[0].source == "scopus"
