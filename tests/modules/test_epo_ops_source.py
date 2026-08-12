"""EPO OPS adapter — OAuth2 token handling, CQL, parsing, quota (Faz 5.2).

Canned responses inline, the module's *own* `requests` monkeypatched
(CLAUDE.md rule 7), `epo_ops_slot` neutralised. Nothing here touches network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.sources import epo_ops_source as epo

# One realistic exchange-document, trimmed to the fields the adapter reads.
# Note the OPS quirks it exercises: `{"$": ...}` text wrappers, a single-item
# array collapsed to a bare object, and each inventor repeated once per name
# format.
_DOC = {
    "bibliographic-data": {
        "publication-reference": {
            "document-id": [
                {
                    "@document-id-type": "docdb",
                    "country": {"$": "EP"},
                    "doc-number": {"$": "1000000"},
                    "kind": {"$": "A1"},
                    "date": {"$": "20240315"},
                },
                {"@document-id-type": "epodoc", "doc-number": {"$": "EP1000000"}},
            ]
        },
        "invention-title": [
            {"@lang": "de", "$": "Neuronales Netzwerk"},
            {"@lang": "en", "$": "Neural network training method"},
        ],
        "parties": {
            "inventors": {
                "inventor": [
                    {
                        "@data-format": "epodoc",
                        "inventor-name": {"name": {"$": "SMITH JANE"}},
                    },
                    {
                        "@data-format": "original",
                        "inventor-name": {"name": {"$": "Smith Jane"}},
                    },
                    {
                        "@data-format": "epodoc",
                        "inventor-name": {"name": {"$": "LEE ALEX"}},
                    },
                ]
            }
        },
        "patent-classifications": {
            "patent-classification": [
                {
                    "section": {"$": "G"},
                    "class": {"$": "06"},
                    "subclass": {"$": "N"},
                    "main-group": {"$": "3"},
                    "subgroup": {"$": "08"},
                }
            ]
        },
    },
    "abstract": {"@lang": "en", "p": {"$": "A method of training a neural network."}},
}


def _envelope(docs):
    return {
        "ops:world-patent-data": {
            "ops:biblio-search": {"ops:search-result": {"exchange-documents": docs}}
        }
    }


class _Resp:
    def __init__(self, payload=None, status_code=200, content=b"{}"):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


@pytest.fixture(autouse=True)
def creds_and_no_throttle(monkeypatch):
    monkeypatch.setenv("EPO_OPS_KEY", "test-key")
    monkeypatch.setenv("EPO_OPS_SECRET", "test-secret")
    monkeypatch.setattr(epo, "epo_ops_slot", lambda: True)
    monkeypatch.setattr(epo, "consume_quota", lambda *a, **k: True)
    epo.reset_token_cache()
    yield
    epo.reset_token_cache()


# ----------------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------------


def test_credentials_need_both_halves(monkeypatch):
    monkeypatch.delenv("EPO_OPS_SECRET", raising=False)
    assert epo.credentials_ok() is False


def test_credentials_are_read_per_call(monkeypatch):
    """A deployment that adds the key must not need a restart."""
    monkeypatch.delenv("EPO_OPS_KEY", raising=False)
    assert epo.credentials_ok() is False
    monkeypatch.setenv("EPO_OPS_KEY", "late-key")
    assert epo.credentials_ok() is True


def test_search_without_credentials_raises_rather_than_returning_empty(monkeypatch):
    """Empty would be recorded as "scanned fine, found nothing" and shown to
    the user as "there is nothing out there"."""
    monkeypatch.delenv("EPO_OPS_KEY", raising=False)
    with pytest.raises(SourceThrottledError):
        epo.search('ti,ab any "x"')


# ----------------------------------------------------------------------------
# CQL
# ----------------------------------------------------------------------------


def test_cql_ors_every_keyword_into_one_query():
    assert epo.build_cql(["neural network", "robotics"]) == (
        'ti,ab any "neural network" or ti,ab any "robotics"'
    )


def test_cql_drops_quotes_from_terms():
    """CQL has no escape for a double quote inside a quoted string, and a
    keyword containing one is a typo, not an operator."""
    assert epo.build_cql(['say "hi"']) == 'ti,ab any "say hi"'


def test_cql_skips_blank_keywords():
    assert epo.build_cql(["", "   ", "ai"]) == 'ti,ab any "ai"'


def test_keyword_search_issues_exactly_one_request(monkeypatch):
    """The reason epo_ops is not in _PER_KEYWORD_REQUEST_SOURCES."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs.get("params", {}).get("q"))
        return _Resp(_envelope([_DOC]))

    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({"access_token": "t"}))
    monkeypatch.setattr(epo.requests, "get", fake_get)

    epo.search_for_keywords(["neural network", "robotics", "cnc"])
    assert len(calls) == 1
    assert calls[0].count(" or ") == 2


def test_no_keywords_makes_no_request(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not have called EPO")

    monkeypatch.setattr(epo.requests, "get", boom)
    assert epo.search_for_keywords(["", "  "]) == []


# ----------------------------------------------------------------------------
# Token cache
# ----------------------------------------------------------------------------


def test_token_is_fetched_once_and_reused(monkeypatch):
    posts = []

    def fake_post(*args, **kwargs):
        posts.append(1)
        return _Resp({"access_token": "tok-1"})

    monkeypatch.setattr(epo.requests, "post", fake_post)
    monkeypatch.setattr(epo.requests, "get", lambda *a, **k: _Resp(_envelope([])))

    epo.search('ti,ab any "a"')
    epo.search('ti,ab any "b"')
    assert len(posts) == 1


def test_expired_token_is_refetched(monkeypatch):
    posts = []

    def fake_post(*args, **kwargs):
        posts.append(1)
        return _Resp({"access_token": f"tok-{len(posts)}"})

    monkeypatch.setattr(epo.requests, "post", fake_post)
    monkeypatch.setattr(epo.requests, "get", lambda *a, **k: _Resp(_envelope([])))

    epo.search('ti,ab any "a"')
    epo._token_cache["expires_at"] = 0  # pretend the TTL elapsed
    epo.search('ti,ab any "b"')
    assert len(posts) == 2


def test_401_refreshes_the_token_once_and_retries(monkeypatch):
    """A cached token can expire between the freshness check and the request
    landing at EPO — one forced retry beats widening the skew margin."""
    posts = []
    auth_headers = []

    def fake_post(*args, **kwargs):
        posts.append(1)
        return _Resp({"access_token": f"tok-{len(posts)}"})

    def fake_get(url, **kwargs):
        auth_headers.append(kwargs["headers"]["Authorization"])
        if len(auth_headers) == 1:
            return _Resp(status_code=401)
        return _Resp(_envelope([_DOC]))

    monkeypatch.setattr(epo.requests, "post", fake_post)
    monkeypatch.setattr(epo.requests, "get", fake_get)

    out = epo.search('ti,ab any "a"')
    assert len(out) == 1
    assert len(posts) == 2
    assert auth_headers == ["Bearer tok-1", "Bearer tok-2"]


def test_persistent_401_gives_up_rather_than_looping(monkeypatch):
    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({"access_token": "t"}))
    monkeypatch.setattr(epo.requests, "get", lambda *a, **k: _Resp(status_code=401))

    with pytest.raises(requests.HTTPError):
        epo.search('ti,ab any "a"')


def test_auth_response_without_a_token_is_an_error(monkeypatch):
    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({}))
    with pytest.raises(ValueError):
        epo._fetch_token()


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------


@pytest.fixture
def one_result(monkeypatch):
    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({"access_token": "t"}))
    monkeypatch.setattr(epo.requests, "get", lambda *a, **k: _Resp(_envelope([_DOC])))
    return epo.search('ti,ab any "neural network"')[0]


def test_publication_number_is_the_external_id(one_result):
    assert one_result.external_id == "EP1000000A1"


def test_english_title_wins_over_other_languages(one_result):
    assert one_result.title == "Neural network training method"


def test_abstract_is_flattened_to_text(one_result):
    assert one_result.abstract == "A method of training a neural network."


def test_inventors_are_deduplicated_across_name_formats(one_result):
    """OPS repeats each party once per name format, which would otherwise
    double every inventor."""
    assert one_result.authors == ["SMITH JANE", "LEE ALEX"]


def test_cpc_codes_become_categories(one_result):
    assert one_result.categories == ["G06N3/08"]


def test_publication_date_is_parsed(one_result):
    assert one_result.published_at == datetime(2024, 3, 15, tzinfo=UTC)


def test_kind_is_patent_and_doi_is_none(one_result):
    assert one_result.kind == "patent"
    # Patents have no DOI; None keeps them out of upsert_paper's DOI branch so
    # they dedup on (source, external_id) — the publication number.
    assert one_result.doi is None


def test_url_points_at_espacenet(one_result):
    assert "espacenet.com" in one_result.url
    assert "EP1000000A1" in one_result.url


def test_record_without_a_publication_number_is_skipped(monkeypatch):
    """Skipped, not fatal — one malformed record must not lose the others."""
    broken = {"bibliographic-data": {"invention-title": {"@lang": "en", "$": "No id"}}}
    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({"access_token": "t"}))
    monkeypatch.setattr(epo.requests, "get", lambda *a, **k: _Resp(_envelope([broken, _DOC])))

    out = epo.search('ti,ab any "x"')
    assert len(out) == 1
    assert out[0].external_id == "EP1000000A1"


def test_single_document_not_wrapped_in_a_list_is_handled(monkeypatch):
    """OPS collapses single-element arrays into the bare object."""
    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({"access_token": "t"}))
    monkeypatch.setattr(epo.requests, "get", lambda *a, **k: _Resp(_envelope(_DOC)))

    assert len(epo.search('ti,ab any "x"')) == 1


def test_404_means_no_matches_not_a_failure(monkeypatch):
    """OPS answers 404 for an empty result set, which is a common outcome for
    a narrow patent query."""
    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({"access_token": "t"}))
    monkeypatch.setattr(epo.requests, "get", lambda *a, **k: _Resp(status_code=404))

    assert epo.search('ti,ab any "x"') == []


def test_server_error_propagates(monkeypatch):
    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({"access_token": "t"}))
    monkeypatch.setattr(epo.requests, "get", lambda *a, **k: _Resp(status_code=503))

    with pytest.raises(requests.HTTPError):
        epo.search('ti,ab any "x"')


# ----------------------------------------------------------------------------
# Quota
# ----------------------------------------------------------------------------


def test_exhausted_quota_raises_before_calling_epo(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not have called EPO with no budget")

    monkeypatch.setattr(epo, "consume_quota", lambda *a, **k: False)
    monkeypatch.setattr(epo.requests, "get", boom)

    with pytest.raises(SourceThrottledError):
        epo.search('ti,ab any "x"')


def test_response_size_is_charged_against_the_byte_budget(monkeypatch):
    charges = []

    def record(name, **kwargs):
        charges.append(kwargs)
        return True

    monkeypatch.setattr(epo, "consume_quota", record)
    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({"access_token": "t"}))
    monkeypatch.setattr(
        epo.requests,
        "get",
        lambda *a, **k: _Resp(_envelope([_DOC]), content=b"x" * 100_000),
    )

    epo.search('ti,ab any "x"')
    # One reservation before the call, one settlement after.
    assert charges[0]["bytes_"] == epo._NOMINAL_BYTES
    assert charges[1]["bytes_"] == 100_000 - epo._NOMINAL_BYTES
    assert charges[1]["cost"] == 0


def test_settlement_never_discards_results_already_paid_for(monkeypatch):
    """The bytes are spent by the time we can measure them; refusing here
    would throw away a response we were already billed for. The *next* call is
    the one that gets refused."""
    calls = {"n": 0}

    def budget(name, **kwargs):
        calls["n"] += 1
        return calls["n"] == 1  # reservation succeeds, settlement reports "over"

    monkeypatch.setattr(epo, "consume_quota", budget)
    monkeypatch.setattr(epo.requests, "post", lambda *a, **k: _Resp({"access_token": "t"}))
    monkeypatch.setattr(epo.requests, "get", lambda *a, **k: _Resp(_envelope([_DOC])))

    assert len(epo.search('ti,ab any "x"')) == 1


def test_rate_limited_slot_raises(monkeypatch):
    monkeypatch.setattr(epo, "epo_ops_slot", lambda: False)
    with pytest.raises(SourceThrottledError):
        epo.search('ti,ab any "x"')
