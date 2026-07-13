"""Source adapters — parsing + registry, all with canned responses (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import requests

from app.modules.scrape.sources import AVAILABLE_SOURCES, enabled_sources
from app.modules.scrape.sources import pubmed_source as pm
from app.modules.scrape.sources import semantic_scholar_source as ss

# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------


def test_registry_has_all_three_sources():
    assert set(AVAILABLE_SOURCES) == {"arxiv", "semantic_scholar", "pubmed"}


def test_enabled_sources_defaults_to_all(monkeypatch):
    monkeypatch.delenv("SCRAPE_SOURCES", raising=False)
    assert set(enabled_sources()) == {"arxiv", "semantic_scholar", "pubmed"}


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
