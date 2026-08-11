"""PatentsView adapter — query DSL, parsing, 429 handling (Faz 5.2).

Canned responses inline, the module's own `requests` monkeypatched,
`patentsview_slot` neutralised. No network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import requests

from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.sources import patentsview_source as pv

_PATENT = {
    "patent_id": "11123456",
    "patent_title": "Method for training a neural network",
    "patent_abstract": "A computer-implemented method of training a neural network.",
    "patent_date": "2024-05-12",
    "inventors": [
        {"inventor_name_first": "Jane", "inventor_name_last": "Smith"},
        {"inventor_name_first": "Alex", "inventor_name_last": "Lee"},
    ],
    "assignees": [{"assignee_organization": "Example Robotics Inc."}],
    "cpc_current": [{"cpc_group_id": "G06N3/08"}, {"cpc_group_id": "G06N3/04"}],
}


class _Resp:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


@pytest.fixture(autouse=True)
def creds_and_no_throttle(monkeypatch):
    monkeypatch.setenv("PATENTSVIEW_API_KEY", "test-key")
    monkeypatch.setattr(pv, "patentsview_slot", lambda: True)
    monkeypatch.setattr(pv, "consume_quota", lambda *a, **k: True)


@pytest.fixture
def one_result(monkeypatch):
    monkeypatch.setattr(pv.requests, "get", lambda *a, **k: _Resp({"patents": [_PATENT]}))
    return pv.search_for_keywords(["neural network"])[0]


# ----------------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------------


def test_credentials_are_read_per_call(monkeypatch):
    monkeypatch.delenv("PATENTSVIEW_API_KEY", raising=False)
    assert pv.credentials_ok() is False
    monkeypatch.setenv("PATENTSVIEW_API_KEY", "late")
    assert pv.credentials_ok() is True


def test_missing_key_raises_rather_than_returning_empty(monkeypatch):
    monkeypatch.delenv("PATENTSVIEW_API_KEY", raising=False)
    with pytest.raises(SourceThrottledError):
        pv.search("robotics")


def test_api_key_is_sent_as_a_header(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs["headers"])
        return _Resp({"patents": []})

    monkeypatch.setattr(pv.requests, "get", fake_get)
    pv.search("robotics")
    assert seen["X-Api-Key"] == "test-key"


# ----------------------------------------------------------------------------
# Query DSL
# ----------------------------------------------------------------------------


def test_single_keyword_searches_title_and_abstract():
    query = pv.build_query(["robotics"])
    assert query == {
        "_or": [
            {"_text_any": {"patent_title": "robotics"}},
            {"_text_any": {"patent_abstract": "robotics"}},
        ]
    }


def test_blank_keywords_produce_no_query():
    assert pv.build_query(["", "   "]) == {}


def test_keyword_search_issues_exactly_one_request(monkeypatch):
    """`_or` carries the whole set — the reason patentsview is not in
    _PER_KEYWORD_REQUEST_SOURCES."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(json.loads(kwargs["params"]["q"]))
        return _Resp({"patents": []})

    monkeypatch.setattr(pv.requests, "get", fake_get)
    pv.search_for_keywords(["a", "b", "c"])

    assert len(calls) == 1
    assert len(calls[0]["_or"]) == 6  # title + abstract clause per keyword


def test_no_keywords_makes_no_request(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not have called PatentsView")

    monkeypatch.setattr(pv.requests, "get", boom)
    assert pv.search_for_keywords([" "]) == []


def test_size_is_capped(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(json.loads(kwargs["params"]["o"]))
        return _Resp({"patents": []})

    monkeypatch.setattr(pv.requests, "get", fake_get)
    pv.search_for_keywords(["x"], max_results=5000)
    assert seen["size"] == pv._MAX_SIZE


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------


def test_external_id_is_prefixed_with_the_country(one_result):
    assert one_result.external_id == "US11123456"


def test_inventors_become_authors(one_result):
    assert one_result.authors == ["Jane Smith", "Alex Lee"]


def test_assignees_ride_in_categories_not_authors(one_result):
    """An organisation is not an inventor, and the card renders authors as
    people — so the owner is carried as a prefixed category instead."""
    assert "assignee:Example Robotics Inc." in one_result.categories
    assert "Example Robotics Inc." not in one_result.authors


def test_cpc_codes_are_categories(one_result):
    assert one_result.categories[:2] == ["G06N3/08", "G06N3/04"]


def test_date_is_parsed(one_result):
    assert one_result.published_at == datetime(2024, 5, 12, tzinfo=UTC)


def test_kind_is_patent_and_doi_is_none(one_result):
    assert one_result.kind == "patent"
    assert one_result.doi is None


def test_url_points_at_google_patents(one_result):
    assert one_result.url == "https://patents.google.com/patent/US11123456"


def test_record_without_an_id_is_skipped(monkeypatch):
    broken = {"patent_title": "No id here"}
    monkeypatch.setattr(pv.requests, "get", lambda *a, **k: _Resp({"patents": [broken, _PATENT]}))
    out = pv.search_for_keywords(["x"])
    assert len(out) == 1


def test_malformed_nested_entries_do_not_raise(monkeypatch):
    """Remote JSON is untrusted — a string where a dict belongs must be
    skipped, not fatal."""
    messy = dict(_PATENT, inventors=["not a dict"], assignees=[None], cpc_current=["nope"])
    monkeypatch.setattr(pv.requests, "get", lambda *a, **k: _Resp({"patents": [messy]}))

    out = pv.search_for_keywords(["x"])
    assert out[0].authors == []
    assert out[0].categories == []


def test_unassigned_patent_is_fine(monkeypatch):
    """Individual inventors file without an assignee; that is normal."""
    solo = dict(_PATENT, assignees=[])
    monkeypatch.setattr(pv.requests, "get", lambda *a, **k: _Resp({"patents": [solo]}))
    assert pv.search_for_keywords(["x"])[0].categories == ["G06N3/08", "G06N3/04"]


# ----------------------------------------------------------------------------
# Throttling
# ----------------------------------------------------------------------------


def test_429_is_retried_once_after_retry_after(monkeypatch):
    slept = []
    calls = []

    def fake_get(url, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _Resp(status_code=429, headers={"Retry-After": "2"})
        return _Resp({"patents": [_PATENT]})

    monkeypatch.setattr(pv.requests, "get", fake_get)
    monkeypatch.setattr(pv.time, "sleep", lambda s: slept.append(s))

    assert len(pv.search_for_keywords(["x"])) == 1
    assert slept == [2.0]


def test_long_retry_after_gives_up_instead_of_parking_a_worker(monkeypatch):
    """The nightly task has its own soft time limit, and "rate limited" is a
    legitimate partial outcome."""
    monkeypatch.setattr(
        pv.requests,
        "get",
        lambda *a, **k: _Resp(status_code=429, headers={"Retry-After": "600"}),
    )
    with pytest.raises(SourceThrottledError):
        pv.search_for_keywords(["x"])


def test_429_without_a_usable_header_gives_up(monkeypatch):
    monkeypatch.setattr(
        pv.requests, "get", lambda *a, **k: _Resp(status_code=429, headers={"Retry-After": "soon"})
    )
    with pytest.raises(SourceThrottledError):
        pv.search_for_keywords(["x"])


def test_exhausted_quota_raises_before_calling(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not have called PatentsView with no budget")

    monkeypatch.setattr(pv, "consume_quota", lambda *a, **k: False)
    monkeypatch.setattr(pv.requests, "get", boom)

    with pytest.raises(SourceThrottledError):
        pv.search_for_keywords(["x"])


def test_server_error_propagates(monkeypatch):
    monkeypatch.setattr(pv.requests, "get", lambda *a, **k: _Resp(status_code=500))
    with pytest.raises(requests.HTTPError):
        pv.search_for_keywords(["x"])
