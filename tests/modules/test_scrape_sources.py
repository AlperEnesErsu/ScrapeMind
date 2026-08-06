"""Source adapters — parsing + registry, all with canned responses (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from app.modules.scrape.net_guard import BLOCKED_MESSAGE, is_public_http_url
from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.sources import AVAILABLE_SOURCES, SOURCE_META, enabled_sources, rss_source
from app.modules.scrape.sources import arxiv_source as ax
from app.modules.scrape.sources import crossref_source as cr
from app.modules.scrape.sources import openalex_source as oa
from app.modules.scrape.sources import pubmed_source as pm
from app.modules.scrape.sources import semantic_scholar_source as ss
from app.modules.scrape.sources import youtube_channel_source as yc

_FEED_KEYS = {f["key"] for f in rss_source.FEEDS}
_REACH_KEYS = {"youtube_reach", "github_reach", "web_reach"}
_CHANNEL_KEYS = {"youtube_channel"}

# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------


def test_registry_has_academic_adapters_and_feeds():
    assert {
        "arxiv",
        "semantic_scholar",
        "pubmed",
        "openalex",
        "crossref",
    } | _FEED_KEYS | _REACH_KEYS | _CHANNEL_KEYS == set(AVAILABLE_SOURCES)


def test_every_feed_key_has_source_meta():
    for key in _FEED_KEYS:
        assert key in SOURCE_META
        assert SOURCE_META[key]["label"]


def test_enabled_sources_defaults_to_all(monkeypatch):
    monkeypatch.delenv("SCRAPE_SOURCES", raising=False)
    assert (
        set(enabled_sources())
        == {"arxiv", "semantic_scholar", "pubmed", "openalex", "crossref"}
        | _FEED_KEYS
        | _REACH_KEYS
        | _CHANNEL_KEYS
    )


def test_enabled_sources_filters_and_ignores_unknown(monkeypatch):
    monkeypatch.setenv("SCRAPE_SOURCES", "arxiv, nope, PubMed")
    assert set(enabled_sources()) == {"arxiv", "pubmed"}


# ----------------------------------------------------------------------------
# arXiv — SDK-based, not requests-based, so faked at the arxiv.Client/Search
# level rather than through a monkeypatched requests.get.
# ----------------------------------------------------------------------------


class _FakeAuthor:
    def __init__(self, name):
        self.name = name


class _FakeArxivResult:
    def __init__(self, *, doi=""):
        self.entry_id = "http://arxiv.org/abs/2401.12345v2"
        self.title = "A Paper\nTitle"
        self.summary = "  An abstract.  "
        self.authors = [_FakeAuthor("A. One")]
        self.pdf_url = "http://arxiv.org/pdf/2401.12345v2"
        self.published = None
        self.categories = ["cs.LG"]
        self.doi = doi


class _FakeArxivClient:
    """Stand-in for arxiv.Client — ignores construction args, `results()`
    just replays whatever was queued via `queued`."""

    queued: list = []

    def __init__(self, *a, **k):
        pass

    def results(self, search):  # noqa: ARG002
        return list(_FakeArxivClient.queued)


def _fake_arxiv(monkeypatch, results):
    """Point arxiv_source at a fake arxiv.Client/Search pair and make the
    rate limiter a no-op, matching the other adapters' rate-limit tests."""
    monkeypatch.setattr(ax, "arxiv_slot", lambda: True)
    _FakeArxivClient.queued = results
    monkeypatch.setattr(ax.arxiv, "Client", _FakeArxivClient)
    monkeypatch.setattr(ax.arxiv, "Search", lambda **kwargs: kwargs)


def test_arxiv_parses_payload_and_normalizes_doi(monkeypatch):
    _fake_arxiv(monkeypatch, [_FakeArxivResult(doi="https://doi.org/10.4321/XYZ")])
    out = ax.search("transformers", max_results=5)
    assert len(out) == 1
    p = out[0]
    assert p.source == "arxiv"
    assert p.external_id == "2401.12345v2"
    assert p.title == "A Paper Title"
    assert p.abstract == "An abstract."
    assert p.doi == "10.4321/xyz"


def test_arxiv_blank_doi_normalizes_to_none(monkeypatch):
    """The SDK returns "" (not None) when a preprint has no DOI yet —
    normalize_doi must turn that into a clean None, not store the blank."""
    _fake_arxiv(monkeypatch, [_FakeArxivResult(doi="")])
    out = ax.search("transformers", max_results=5)
    assert out[0].doi is None


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
    "externalIds": {"DOI": "https://doi.org/10.5555/ATTN"},
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
    # externalIds.DOI comes back as a doi.org URL; normalized on the way in.
    assert p.doi == "10.5555/attn"


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
        (oa, "openalex_slot", "x"),
        (cr, "crossref_slot", "x"),
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
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">12345678</ArticleId>
        <ArticleId IdType="doi">10.1234/CRISPR.2026.001</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

_PUBMED_XML_ELOCATION_DOI = b"""<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">99999999</PMID>
      <Article>
        <ELocationID EIdType="doi">10.9876/fallback</ELocationID>
        <ArticleTitle>A Fallback DOI Case.</ArticleTitle>
      </Article>
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
    assert p.doi == "10.1234/crispr.2026.001"


def test_pubmed_falls_back_to_elocation_doi(monkeypatch):
    """When PubmedData/ArticleIdList has no doi-typed entry (records indexed
    before PubMed backfilled it), the publisher-supplied ELocationID on the
    Article itself is used instead."""
    responses = iter(
        [
            _fake_response(json_data={"esearchresult": {"idlist": ["99999999"]}}),
            _fake_response(content=_PUBMED_XML_ELOCATION_DOI),
        ]
    )
    monkeypatch.setattr(pm.requests, "get", lambda *a, **k: next(responses))
    out = pm.search("crispr", max_results=5)
    assert len(out) == 1
    assert out[0].doi == "10.9876/fallback"


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
# OpenAlex
# ----------------------------------------------------------------------------

_OA_ITEM = {
    "id": "https://openalex.org/W2741809807",
    "display_name": "Attention Is\nAll You Need",
    "doi": "https://doi.org/10.5555/attn2",
    "publication_date": "2017-06-12",
    "publication_year": 2017,
    "authorships": [
        {"author": {"display_name": "Ashish Vaswani"}},
        {"author": {"display_name": ""}},
    ],
    "primary_location": {
        "landing_page_url": "https://example.com/landing",
        "pdf_url": None,
    },
    "best_oa_location": {"pdf_url": "https://example.com/paper.pdf"},
    "topics": [{"display_name": "Machine Learning"}],
    "concepts": [{"display_name": "Should not be used"}],
    "abstract_inverted_index": {"We": [0], "propose": [1], "the": [2], "Transformer.": [3]},
}


def test_openalex_parses_payload(monkeypatch):
    monkeypatch.setattr(
        oa.requests, "get", lambda *a, **k: _fake_response(json_data={"results": [_OA_ITEM]})
    )
    out = oa.search("transformers", max_results=5)
    assert len(out) == 1
    p = out[0]
    assert p.source == "openalex"
    # Host stripped off the full OpenAlex id URL
    assert p.external_id == "W2741809807"
    assert p.title == "Attention Is All You Need"  # newline flattened
    assert p.doi == "10.5555/attn2"
    assert p.abstract == "We propose the Transformer."
    assert p.authors == ["Ashish Vaswani"]  # blank name dropped
    # doi resolver link wins over primary_location.landing_page_url
    assert p.url == "https://doi.org/10.5555/attn2"
    assert p.pdf_url == "https://example.com/paper.pdf"
    assert p.published_at.tzinfo is not None
    assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2017, 6, 12)
    # topics preferred over concepts when both are present
    assert p.categories == ["Machine Learning"]


def test_openalex_url_falls_back_to_landing_page_then_id(monkeypatch):
    item = {**_OA_ITEM, "doi": None}
    monkeypatch.setattr(
        oa.requests, "get", lambda *a, **k: _fake_response(json_data={"results": [item]})
    )
    p = oa.search("x", max_results=1)[0]
    assert p.url == "https://example.com/landing"

    item2 = {**item, "primary_location": {}}
    monkeypatch.setattr(
        oa.requests, "get", lambda *a, **k: _fake_response(json_data={"results": [item2]})
    )
    p2 = oa.search("x", max_results=1)[0]
    assert p2.url == "https://openalex.org/W2741809807"


def test_openalex_date_falls_back_to_year(monkeypatch):
    item = {**_OA_ITEM, "publication_date": None}
    monkeypatch.setattr(
        oa.requests, "get", lambda *a, **k: _fake_response(json_data={"results": [item]})
    )
    p = oa.search("x", max_results=1)[0]
    assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2017, 1, 1)


def test_openalex_skips_records_without_id_or_title():
    assert oa._to_payload({**_OA_ITEM, "id": ""}) is None
    assert oa._to_payload({**_OA_ITEM, "display_name": "", "title": ""}) is None


def test_openalex_search_empty_query_makes_no_http_call(monkeypatch):
    calls = []
    monkeypatch.setattr(oa.requests, "get", lambda *a, **k: calls.append(1))
    assert oa.search("") == []
    assert oa.search("   ") == []
    assert calls == []


def test_openalex_keywords_build_or_query(monkeypatch):
    seen = {}

    def fake_search(query, *, max_results):
        seen["query"] = query
        return []

    monkeypatch.setattr(oa, "search", fake_search)
    oa.search_for_keywords(["gene editing", "crispr"], max_results=5)
    assert seen["query"] == "gene editing OR crispr"


def test_openalex_keywords_empty_list_returns_empty_without_call(monkeypatch):
    calls = []
    monkeypatch.setattr(oa, "search", lambda *a, **k: calls.append(1))
    assert oa.search_for_keywords([], max_results=5) == []
    assert oa.search_for_keywords(["", "  "], max_results=5) == []
    assert calls == []


class TestDecodeAbstract:
    def test_none_returns_none(self):
        assert oa._decode_abstract(None) is None

    def test_empty_dict_returns_none(self):
        assert oa._decode_abstract({}) is None

    def test_single_word(self):
        assert oa._decode_abstract({"Hello": [0]}) == "Hello"

    def test_out_of_order_positions_reassembled(self):
        inverted = {"quick": [1], "The": [0], "fox": [3], "brown": [2]}
        assert oa._decode_abstract(inverted) == "The quick brown fox"

    def test_non_list_value_does_not_raise(self):
        inverted = {"good": [0], "bad": "not-a-list"}
        assert oa._decode_abstract(inverted) == "good"


# ----------------------------------------------------------------------------
# Crossref
# ----------------------------------------------------------------------------

_CR_ITEM = {
    "DOI": "10.5555/CROSSREF.2024.001",
    "title": ["Attention Is\nAll You Need"],
    "abstract": "<jats:p>We propose the <jats:italic>Transformer</jats:italic> &amp; friends.</jats:p>",
    "author": [
        {"given": "Ashish", "family": "Vaswani"},
        {"name": "The Consortium"},
        {"given": "", "family": ""},
    ],
    "issued": {"date-parts": [[2024, 3, 15]]},
    "URL": "https://doi.org/10.5555/crossref.2024.001",
    "link": [
        {"URL": "https://example.test/paper.xml", "content-type": "application/xml"},
        {"URL": "https://example.test/paper.pdf", "content-type": "application/pdf"},
    ],
    "subject": ["Machine Learning", "Computer Science"],
    "type": "journal-article",
    "container-title": ["Journal of Examples"],
}


def test_crossref_parses_payload(monkeypatch):
    monkeypatch.setattr(
        cr.requests,
        "get",
        lambda *a, **k: _fake_response(json_data={"message": {"items": [_CR_ITEM]}}),
    )
    out = cr.search("transformers", max_results=5)
    assert len(out) == 1
    p = out[0]
    assert p.source == "crossref"
    # Normalized DOI is both external_id and doi
    assert p.external_id == "10.5555/crossref.2024.001"
    assert p.doi == "10.5555/crossref.2024.001"
    assert p.title == "Attention Is All You Need"  # newline flattened
    assert p.abstract == "We propose the Transformer & friends."
    # given+family joined, org "name" author kept, blank author dropped
    assert p.authors == ["Ashish Vaswani", "The Consortium"]
    assert p.url == "https://doi.org/10.5555/crossref.2024.001"
    # First entry is application/xml (skipped), second is the PDF
    assert p.pdf_url == "https://example.test/paper.pdf"
    assert p.published_at.tzinfo is not None
    assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2024, 3, 15)
    assert p.categories == ["Machine Learning", "Computer Science"]


def test_crossref_skips_records_without_doi():
    assert cr._to_payload({**_CR_ITEM, "DOI": None}) is None
    assert cr._to_payload({**_CR_ITEM, "DOI": "not-a-doi"}) is None


def test_crossref_skips_records_without_title():
    assert cr._to_payload({**_CR_ITEM, "title": []}) is None
    assert cr._to_payload({**_CR_ITEM, "title": [""]}) is None


def test_crossref_pdf_url_none_when_no_pdf_link():
    item = {
        **_CR_ITEM,
        "link": [{"URL": "https://example.test/x.xml", "content-type": "application/xml"}],
    }
    assert cr._to_payload(item).pdf_url is None


def test_crossref_pdf_url_guards_non_dict_link_entries():
    item = {
        **_CR_ITEM,
        "link": [
            "not-a-dict",
            {"URL": "https://example.test/p.pdf", "content-type": "application/pdf"},
        ],
    }
    assert cr._to_payload(item).pdf_url == "https://example.test/p.pdf"


def test_crossref_search_empty_query_makes_no_http_call(monkeypatch):
    calls = []
    monkeypatch.setattr(cr.requests, "get", lambda *a, **k: calls.append(1))
    assert cr.search("") == []
    assert cr.search("   ") == []
    assert calls == []


def test_crossref_keywords_issue_one_request_per_keyword_and_dedupe(monkeypatch):
    calls = []

    def fake_search(query, *, max_results):
        calls.append(query)
        if query == "bad":
            raise requests.ConnectionError("boom")
        return [cr._to_payload(_CR_ITEM)]

    monkeypatch.setattr(cr, "search", fake_search)
    out = cr.search_for_keywords(["good", "bad", "good2"], max_results=10)
    # Same DOI from two healthy keywords -> deduped to one payload.
    assert len(out) == 1
    assert calls == ["good", "bad", "good2"]


def test_crossref_raises_when_every_keyword_failed(monkeypatch):
    """Same contract as Semantic Scholar's: a wholly-throttled/failed source
    must not look like "no papers on your topic". Crossref's public pool is
    unauthenticated and shared, so a run of 429s/5xxs is plausible and must
    surface as a raised error, not `status="ok", hits=0`."""

    def fake_search(query, *, max_results):  # noqa: ARG001
        raise requests.HTTPError("429 Client Error")

    monkeypatch.setattr(cr, "search", fake_search)
    with pytest.raises(requests.HTTPError):
        cr.search_for_keywords(["a", "b"], max_results=10)


def test_crossref_partial_success_still_returns(monkeypatch):
    def fake_search(query, *, max_results):  # noqa: ARG001
        if query == "bad":
            raise requests.HTTPError("429 Client Error")
        return [cr._to_payload(_CR_ITEM)]

    monkeypatch.setattr(cr, "search", fake_search)
    assert len(cr.search_for_keywords(["bad", "good"], max_results=10)) == 1


def test_crossref_keywords_empty_list_returns_empty_without_call(monkeypatch):
    calls = []
    monkeypatch.setattr(cr, "search", lambda *a, **k: calls.append(1))
    assert cr.search_for_keywords([], max_results=5) == []
    assert cr.search_for_keywords(["", "  "], max_results=5) == []
    assert calls == []


class TestCrossrefDateParts:
    def test_year_only(self):
        item = {**_CR_ITEM, "issued": {"date-parts": [[2024]]}}
        p = cr._to_payload(item)
        assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2024, 1, 1)

    def test_year_month(self):
        item = {**_CR_ITEM, "issued": {"date-parts": [[2024, 3]]}}
        p = cr._to_payload(item)
        assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2024, 3, 1)

    def test_year_month_day(self):
        item = {**_CR_ITEM, "issued": {"date-parts": [[2024, 3, 15]]}}
        p = cr._to_payload(item)
        assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2024, 3, 15)

    def test_garbage_date_parts_does_not_raise(self):
        item = {**_CR_ITEM, "issued": {"date-parts": [[None]]}}
        p = cr._to_payload(item)
        assert p.published_at is None

    def test_falls_back_to_published_print(self):
        item = {**_CR_ITEM, "issued": {}, "published-print": {"date-parts": [[2020, 5]]}}
        p = cr._to_payload(item)
        assert (p.published_at.year, p.published_at.month) == (2020, 5)

    def test_falls_back_to_published_online(self):
        item = {
            **_CR_ITEM,
            "issued": {},
            "published-print": {},
            "published-online": {"date-parts": [[2019]]},
        }
        p = cr._to_payload(item)
        assert p.published_at.year == 2019

    def test_no_date_anywhere_returns_none(self):
        item = {**_CR_ITEM, "issued": {}}
        p = cr._to_payload(item)
        assert p.published_at is None


class TestStripJats:
    def test_none_returns_none(self):
        assert cr._strip_jats(None) is None

    def test_empty_string_returns_none(self):
        assert cr._strip_jats("") is None

    def test_tags_removed(self):
        assert cr._strip_jats("<jats:p>Some text</jats:p>") == "Some text"

    def test_entities_unescaped(self):
        assert cr._strip_jats("<jats:p>Cats &amp; dogs</jats:p>") == "Cats & dogs"

    def test_whitespace_collapsed(self):
        assert cr._strip_jats("<jats:p>Too   much\n\nwhitespace</jats:p>") == "Too much whitespace"

    def test_tags_only_string_returns_none(self):
        assert cr._strip_jats("<jats:p></jats:p>") is None

    def test_leading_abstract_heading_dropped(self):
        assert (
            cr._strip_jats("<jats:title>Abstract</jats:title><jats:p>Body text.</jats:p>")
            == "Body text."
        )


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


def test_fetch_similar_papers_returns_payloads(monkeypatch):
    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"recommendedPapers": [_SS_ITEM]},
    )
    monkeypatch.setattr(requests, "get", lambda *a, **kw: fake_resp)
    recs = ss.fetch_similar_papers("10.1000/182")
    assert len(recs) == 1
    assert recs[0].title == "Attention Is All You Need"  # newline flattened, like search()


# ----------------------------------------------------------------------------
# YouTube channels (youtube_channel_source) — channel resolution, channel-feed
# ingestion via rss_source.fetch_feed_conditional, and yt-dlp transcripts.
#
# `yc.requests` and `rss_source.requests` are the same module object (both did
# a plain `import requests`), so monkeypatching `yc.requests.get` also serves
# the calls `fetch_channel_videos` makes through `rss_source.fetch_feed_conditional`.
# The SSRF guard, however, is imported by *name* into each module's own
# namespace, so both `yc.is_public_http_url` and `rss_source.is_public_http_url`
# need neutralising separately.
# ----------------------------------------------------------------------------

_CHANNEL_ID = "UC" + "A1b2C3d4E5f6G7h8I9j0K1"  # 24 chars: "UC" + 22


def _allow_yc(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(yc, "is_public_http_url", lambda url, **kw: (True, None))


def _channel_feed_xml(
    channel_id: str, *, video_id: str = "vid1234567A", title: str = "A Great Video"
) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
<link rel="self" href="https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"/>
<id>yt:channel:{channel_id}</id>
<yt:channelId>{channel_id}</yt:channelId>
<title>Fake Channel</title>
<entry>
  <id>yt:video:{video_id}</id>
  <yt:videoId>{video_id}</yt:videoId>
  <yt:channelId>{channel_id}</yt:channelId>
  <title>{title}</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v={video_id}"/>
  <author>
    <name>Fake Channel</name>
    <uri>https://www.youtube.com/channel/{channel_id}</uri>
  </author>
  <published>2026-07-20T12:00:00+00:00</published>
  <updated>2026-07-20T12:05:00+00:00</updated>
  <media:group>
    <media:title>{title}</media:title>
    <media:description>A cool video about   things.  &amp; stuff.</media:description>
  </media:group>
</entry>
</feed>
"""


# ---- resolve_channel --------------------------------------------------------


def test_resolve_channel_direct_channel_url_makes_no_page_request(monkeypatch):
    _allow_yc(monkeypatch)

    def fake_get(url, **kwargs):
        if "feeds/videos.xml" in url:
            return _FakeResponse(_channel_feed_xml(_CHANNEL_ID))
        raise AssertionError(f"unexpected page request to {url}")

    monkeypatch.setattr(yc.requests, "get", fake_get)
    result, err = yc.resolve_channel(f"https://www.youtube.com/channel/{_CHANNEL_ID}")
    assert err is None
    assert result["channel_id"] == _CHANNEL_ID
    assert result["title"] == "Fake Channel"
    assert result["url"] == f"https://www.youtube.com/channel/{_CHANNEL_ID}"


def test_resolve_channel_bare_id_makes_no_page_request(monkeypatch):
    _allow_yc(monkeypatch)

    def fake_get(url, **kwargs):
        if "feeds/videos.xml" in url:
            return _FakeResponse(_channel_feed_xml(_CHANNEL_ID))
        raise AssertionError(f"unexpected page request to {url}")

    monkeypatch.setattr(yc.requests, "get", fake_get)
    result, err = yc.resolve_channel(_CHANNEL_ID)
    assert err is None
    assert result["channel_id"] == _CHANNEL_ID


def test_resolve_channel_extracts_id_from_channel_id_json(monkeypatch):
    html = f'<html><script>var x = {{"channelId":"{_CHANNEL_ID}","other":"x"}};</script></html>'
    _allow_yc(monkeypatch)

    def fake_get(url, **kwargs):
        if "feeds/videos.xml" in url:
            return _FakeResponse(_channel_feed_xml(_CHANNEL_ID))
        return _FakeResponse(html)

    monkeypatch.setattr(yc.requests, "get", fake_get)
    result, err = yc.resolve_channel("@somehandle")
    assert err is None
    assert result["channel_id"] == _CHANNEL_ID


def test_resolve_channel_extracts_id_from_meta_identifier_fallback(monkeypatch):
    html = f'<html><head><meta itemprop="identifier" content="{_CHANNEL_ID}"></head></html>'
    _allow_yc(monkeypatch)

    def fake_get(url, **kwargs):
        if "feeds/videos.xml" in url:
            return _FakeResponse(_channel_feed_xml(_CHANNEL_ID))
        return _FakeResponse(html)

    monkeypatch.setattr(yc.requests, "get", fake_get)
    result, err = yc.resolve_channel("https://www.youtube.com/c/SomeChannel")
    assert err is None
    assert result["channel_id"] == _CHANNEL_ID


def test_resolve_channel_garbage_input_returns_error_without_raising(monkeypatch):
    calls = []
    monkeypatch.setattr(yc.requests, "get", lambda *a, **k: calls.append(1))
    result, err = yc.resolve_channel("this is not a channel of any kind")
    assert result is None
    assert isinstance(err, str) and err
    assert calls == []

    result2, err2 = yc.resolve_channel("")
    assert result2 is None
    assert err2


def test_resolve_channel_rejects_ssrf_blocked_url(monkeypatch):
    monkeypatch.setattr(yc, "is_public_http_url", lambda url, **kw: (False, BLOCKED_MESSAGE))
    calls = []
    monkeypatch.setattr(yc.requests, "get", lambda *a, **k: calls.append(1))
    result, err = yc.resolve_channel("@somehandle")
    assert result is None
    assert err
    assert calls == []


# ---- fetch_channel_videos ----------------------------------------------------


def test_fetch_channel_videos_maps_entries_to_payloads(monkeypatch):
    _allow_yc(monkeypatch)
    monkeypatch.setattr(
        yc.requests, "get", lambda *a, **k: _FakeResponse(_channel_feed_xml(_CHANNEL_ID))
    )
    payloads, status, _etag, _last_modified, title = yc.fetch_channel_videos(_CHANNEL_ID)
    assert status == "ok"
    assert title == "Fake Channel"
    assert len(payloads) == 1
    p = payloads[0]
    assert p.source == "youtube_channel"
    assert p.kind == "video"
    assert p.external_id == "vid1234567A"
    assert p.url == "https://www.youtube.com/watch?v=vid1234567A"
    assert p.authors == ["Fake Channel"]
    assert p.abstract == "A cool video about things. & stuff."
    assert p.categories == ["video"]
    assert p.published_at is not None
    assert p.published_at.tzinfo is not None
    assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2026, 7, 20)


def test_fetch_channel_videos_passes_not_modified_through(monkeypatch):
    _allow_yc(monkeypatch)
    monkeypatch.setattr(yc.requests, "get", lambda *a, **k: _FakeResponse("", status_code=304))
    payloads, status, etag, _last_modified, _title = yc.fetch_channel_videos(
        _CHANNEL_ID, etag='W/"abc"'
    )
    assert status == "not_modified"
    assert payloads == []
    assert etag == 'W/"abc"'


def test_entry_without_video_id_is_skipped():
    entry = {"title": "Untitled Mystery", "link": "https://www.youtube.com/watch?list=PL123"}
    assert yc._entry_to_payload(entry) is None


# ---- fetch_transcript --------------------------------------------------------


def _ytdlp_proc(info: dict, *, returncode: int = 0):
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(info), stderr="")


def test_fetch_transcript_parses_json3(monkeypatch):
    caption_json = {"events": [{"segs": [{"utf8": "Hello "}]}, {"segs": [{"utf8": "world."}]}]}
    info = {"subtitles": {"en": [{"ext": "json3", "url": "https://example.test/caps.json3"}]}}
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: True)
    monkeypatch.setattr(yc.subprocess, "run", lambda *a, **k: _ytdlp_proc(info))
    monkeypatch.setattr(yc, "is_public_http_url", lambda url, **kw: (True, None))
    monkeypatch.setattr(yc.requests, "get", lambda *a, **k: _FakeResponse(json.dumps(caption_json)))
    text = yc.fetch_transcript("vid123", languages=("en",))
    assert text == "Hello world."


def test_fetch_transcript_parses_vtt_and_dedupes_scroll_lines(monkeypatch):
    vtt_body = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "Hello <c>there</c>\n\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "Hello there\n"
        "how are you\n"
    )
    info = {"subtitles": {"en": [{"ext": "vtt", "url": "https://example.test/caps.vtt"}]}}
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: True)
    monkeypatch.setattr(yc.subprocess, "run", lambda *a, **k: _ytdlp_proc(info))
    monkeypatch.setattr(yc, "is_public_http_url", lambda url, **kw: (True, None))
    monkeypatch.setattr(yc.requests, "get", lambda *a, **k: _FakeResponse(vtt_body))
    text = yc.fetch_transcript("vid123", languages=("en",))
    # "Hello there" from the second cue is a scroll-repeat of the first and
    # must not appear twice.
    assert text == "Hello there how are you"


def test_fetch_transcript_prefers_subtitles_over_automatic_and_matches_en_orig(monkeypatch):
    urls = {
        "https://example.test/sub.json3": {"events": [{"segs": [{"utf8": "Human captions."}]}]},
        "https://example.test/auto.json3": {"events": [{"segs": [{"utf8": "Auto captions."}]}]},
    }
    info_both = {
        "subtitles": {"en": [{"ext": "json3", "url": "https://example.test/sub.json3"}]},
        "automatic_captions": {
            "en-orig": [{"ext": "json3", "url": "https://example.test/auto.json3"}]
        },
    }
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: True)
    monkeypatch.setattr(yc, "is_public_http_url", lambda url, **kw: (True, None))
    monkeypatch.setattr(yc.requests, "get", lambda url, **k: _FakeResponse(json.dumps(urls[url])))

    monkeypatch.setattr(yc.subprocess, "run", lambda *a, **k: _ytdlp_proc(info_both))
    assert yc.fetch_transcript("vid", languages=("en",)) == "Human captions."

    # No human subtitles for "en" at all -> falls back to auto captions,
    # matching the "en-orig" variant via the language-tag prefix match.
    info_auto_only = {
        "subtitles": {},
        "automatic_captions": {
            "en-orig": [{"ext": "json3", "url": "https://example.test/auto.json3"}]
        },
    }
    monkeypatch.setattr(yc.subprocess, "run", lambda *a, **k: _ytdlp_proc(info_auto_only))
    assert yc.fetch_transcript("vid", languages=("en",)) == "Auto captions."


def test_fetch_transcript_returns_none_when_ytdlp_missing(monkeypatch):
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: True)

    def raise_fnf(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(yc.subprocess, "run", raise_fnf)
    assert yc.fetch_transcript("vid") is None


def test_fetch_transcript_returns_none_on_nonzero_returncode(monkeypatch):
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: True)
    monkeypatch.setattr(
        yc.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert yc.fetch_transcript("vid") is None


def test_fetch_transcript_returns_none_on_timeout(monkeypatch):
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: True)

    def raise_timeout(*a, **k):
        raise yc.subprocess.TimeoutExpired(cmd="yt-dlp", timeout=30)

    monkeypatch.setattr(yc.subprocess, "run", raise_timeout)
    assert yc.fetch_transcript("vid") is None


def test_fetch_transcript_returns_none_on_unparseable_json(monkeypatch):
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: True)
    monkeypatch.setattr(
        yc.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="not json at all", stderr=""),
    )
    assert yc.fetch_transcript("vid") is None


def test_fetch_transcript_returns_none_when_no_captions_in_requested_languages(monkeypatch):
    info = {"subtitles": {"fr": [{"ext": "json3", "url": "https://example.test/fr.json3"}]}}
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: True)
    monkeypatch.setattr(yc.subprocess, "run", lambda *a, **k: _ytdlp_proc(info))
    assert yc.fetch_transcript("vid", languages=("tr", "en")) is None


def test_fetch_transcript_output_is_capped(monkeypatch):
    long_text = "word " * 20000
    caption_json = {"events": [{"segs": [{"utf8": long_text}]}]}
    info = {"subtitles": {"en": [{"ext": "json3", "url": "https://example.test/long.json3"}]}}
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: True)
    monkeypatch.setattr(yc.subprocess, "run", lambda *a, **k: _ytdlp_proc(info))
    monkeypatch.setattr(yc, "is_public_http_url", lambda url, **kw: (True, None))
    monkeypatch.setattr(yc.requests, "get", lambda *a, **k: _FakeResponse(json.dumps(caption_json)))
    text = yc.fetch_transcript("vid", languages=("en",))
    assert len(text) == yc.TRANSCRIPT_MAX_CHARS


def test_fetch_transcript_throttled_returns_none_without_subprocess(monkeypatch):
    monkeypatch.setattr(yc, "youtube_channel_slot", lambda: False)
    calls = []
    monkeypatch.setattr(yc.subprocess, "run", lambda *a, **k: calls.append(1))
    assert yc.fetch_transcript("vid") is None
    assert calls == []


# ---- contract no-ops ---------------------------------------------------------


def test_youtube_channel_search_and_search_for_keywords_are_noops():
    assert yc.search("anything", max_results=5) == []
    assert yc.search_for_keywords(["anything"], max_results=5) == []
