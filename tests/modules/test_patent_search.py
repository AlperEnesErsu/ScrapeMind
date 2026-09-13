"""Patent full-text search (Faz 8.4).

The load-bearing properties:

- claims are their own scope, and a claim match outranks a description match
  (what a patent asserts versus what it mentions);
- a stopword-only query is reported as ignored, never as "no results" — that
  would read as "nobody claims this";
- highlighted snippets escape patent text before adding <mark>;
- user text cannot act as a LIKE wildcard, and malformed query syntax is not
  a 500;
- paging keeps a query containing `&` intact.
"""

from __future__ import annotations

from datetime import date

import pytest
from werkzeug.datastructures import MultiDict

from app.modules.patent import search
from app.modules.patent.models import PatentClaim, PatentDocument


@pytest.fixture(autouse=True)
def clean(db):
    PatentDocument.query.delete()
    db.session.commit()
    yield
    PatentDocument.query.delete()
    db.session.commit()


def _doc(
    db,
    number,
    *,
    title="A patent",
    abstract="",
    description="",
    claims=(),
    cpc=("G06N3/08",),
    assignees=("Example AI Inc.",),
    grant=date(2026, 9, 8),
):
    doc = PatentDocument(
        doc_number=number,
        country="US",
        title=title,
        abstract=abstract,
        description=description,
        cpc_codes=list(cpc),
        assignees=list(assignees),
        grant_date=grant,
        ai_source="cpc_core",
        claim_count=len(claims),
    )
    db.session.add(doc)
    db.session.flush()
    for i, text in enumerate(claims, start=1):
        db.session.add(
            PatentClaim(patent_document_id=doc.id, number=i, is_independent=i == 1, text=text)
        )
    db.session.commit()
    return doc


def _run(**args):
    filters = search.SearchFilters.from_args(MultiDict(args))
    return [d.doc_number for d in search.build_query(filters).all()]


class TestFilters:
    def test_bad_input_narrows_nothing_instead_of_failing(self):
        f = search.SearchFilters.from_args(
            MultiDict({"from": "not-a-date", "scope": "everything", "cpc": "g06n%'; drop"})
        )
        assert f.granted_from is None
        assert f.scope == "all"
        assert f.cpc == "G06NDROP"  # only [A-Z0-9/] survives

    def test_empty_filters_are_empty(self):
        assert search.SearchFilters.from_args(MultiDict({})).is_empty


class TestText:
    def test_description_match(self, db):
        _doc(db, "US1B2", description="Quantised attention weights reduce memory.")
        _doc(db, "US2B2", description="A soil density probe.")
        assert _run(q="attention") == ["US1B2"]

    def test_stemming_matches_word_forms(self, db):
        _doc(db, "US1B2", description="The model was quantized after training.")
        assert _run(q="quantizing") == ["US1B2"]

    def test_claims_scope_ignores_description_only_mentions(self, db):
        _doc(db, "US1B2", description="Unlike attention models, this uses a lookup.")
        _doc(db, "US2B2", claims=["A method comprising computing attention weights."])
        assert _run(q="attention", scope="claims") == ["US2B2"]
        assert sorted(_run(q="attention")) == ["US1B2", "US2B2"]

    def test_a_claim_match_outranks_a_description_match(self, db):
        """Even a description that says the term far more often loses to a
        patent that actually claims it."""
        _doc(db, "US1B2", description="attention " * 40)
        _doc(db, "US2B2", claims=["A method of computing attention."])
        assert _run(q="attention")[0] == "US2B2"

    def test_websearch_syntax_phrase_and_exclusion(self, db):
        _doc(db, "US1B2", description="attention weights for images")
        _doc(db, "US2B2", description="attention weights for audio")
        _doc(db, "US3B2", description="weights attention swapped")
        assert sorted(_run(q='"attention weights" -images')) == ["US2B2"]

    @pytest.mark.parametrize("q", ["a & (b", '"unbalanced', "!!!", "a:*|&"])
    def test_malformed_syntax_is_not_an_error(self, db, q):
        _doc(db, "US1B2", description="anything")
        _run(q=q)  # must not raise

    def test_stopword_only_query_is_flagged_not_matched(self, db):
        _doc(db, "US1B2", description="the of and")
        assert not search.query_is_meaningful("the of and")
        assert search.query_is_meaningful("attention")


class TestStructuredFilters:
    def test_cpc_is_a_prefix_match(self, db):
        _doc(db, "US1B2", cpc=["G06N3/08"])
        _doc(db, "US2B2", cpc=["G06V10/82"])
        assert _run(cpc="G06N") == ["US1B2"]
        assert _run(cpc="G06N3/08") == ["US1B2"]

    def test_assignee_wildcards_are_literal(self, db):
        """`100%` must not match every assignee."""
        _doc(db, "US1B2", assignees=["Acme 100% Robotics"])
        _doc(db, "US2B2", assignees=["Other Corp"])
        assert _run(assignee="100%") == ["US1B2"]
        assert _run(assignee="_") == []

    def test_assignee_is_case_insensitive(self, db):
        _doc(db, "US1B2", assignees=["Example AI Inc."])
        assert _run(assignee="example ai") == ["US1B2"]

    def test_grant_date_range(self, db):
        _doc(db, "US1B2", grant=date(2026, 8, 25))
        _doc(db, "US2B2", grant=date(2026, 9, 8))
        assert _run(**{"from": "2026-09-01"}) == ["US2B2"]
        assert _run(to="2026-09-01") == ["US1B2"]


class TestSnippets:
    def test_claim_hit_is_preferred_and_the_lowest_claim_wins(self, db):
        doc = _doc(
            db,
            "US1B2",
            description="attention is mentioned here too",
            claims=["A method of attention.", "The method of claim 1 with attention."],
        )
        snip = search.snippets_for([doc], "attention")[doc.id]
        assert snip.claim_number == 1
        assert "<mark>" in snip.text

    def test_description_hit_when_no_claim_matches(self, db):
        doc = _doc(db, "US1B2", description="Quantised attention.", claims=["A probe."])
        snip = search.snippets_for([doc], "attention")[doc.id]
        assert snip.claim_number is None
        assert "<mark>attention</mark>" in str(snip.text).lower()

    def test_escaping_happens_before_the_marks_are_added(self):
        """Tested on `_highlight` directly rather than through Postgres:
        `ts_headline` happens to drop tag-shaped tokens, and a test that only
        passes because of that would stop protecting anything the day a
        different parser or config keeps them."""
        raw = f'<script>x</script> {search._HL_START}attention{search._HL_END} <b onclick="y">'
        text = str(search._highlight(raw))
        assert "<script>" not in text and 'onclick="' not in text
        assert "&lt;script&gt;" in text
        assert "<mark>attention</mark>" in text

    def test_markup_like_patent_text_survives_escaped(self, db):
        doc = _doc(db, "US1B2", claims=["wherein a < b & c > d for attention layers"])
        text = str(search.snippets_for([doc], "attention")[doc.id].text)
        assert "&lt;" in text and "&amp;" in text
        assert "<mark>attention</mark>" in text


class TestPage:
    def test_blank_page_prompts_instead_of_listing(self, auth_client):
        client, _ = auth_client
        body = client.get("/patents/search").get_data(as_text=True)
        assert "Pencerede aramak için bir terim ya da filtre girin." in body

    def test_stopword_query_warns_instead_of_claiming_no_match(self, auth_client, db):
        client, _ = auth_client
        _doc(db, "US1B2", description="attention")
        body = client.get("/patents/search?q=the+of").get_data(as_text=True)
        assert "yok sayıldı" in body

    def test_no_match_is_scoped_to_the_window(self, auth_client, db):
        client, _ = auth_client
        _doc(db, "US1B2", description="attention")
        body = client.get("/patents/search?q=zeppelin").get_data(as_text=True)
        assert "ABD dışı patentler hakkında bir şey söylemez" in body

    def test_paging_keeps_a_query_containing_an_ampersand(self, auth_client, db):
        """The shared pagination partial used to HTML-escape but not URL-encode,
        so `R&D` split into `q=R` and a stray `D` parameter on page 2."""
        client, _ = auth_client
        for i in range(search.PER_PAGE + 1):
            _doc(db, f"US{i}B2", description="R&D attention pipeline")
        body = client.get("/patents/search?q=R%26D+attention").get_data(as_text=True)
        assert "q=R%26D%20attention" in body
        assert "&amp;D" not in body.split('class="pagination')[1]

    def test_results_link_to_the_detail_page(self, auth_client, db):
        client, _ = auth_client
        _doc(db, "US11123456B2", description="attention")
        body = client.get("/patents/search?q=attention").get_data(as_text=True)
        assert "/patents/US11123456B2" in body


def test_claim_match_outranks_even_an_extreme_description(db):
    """The ordering guarantee must hold by construction. Without normalisation
    `ts_rank` is unbounded, so a long enough repetition could overtake a patent
    that actually claims the term."""
    _doc(
        db,
        "US1B2",
        title="attention attention",
        abstract="attention " * 50,
        description="attention " * 5000,
    )
    _doc(db, "US2B2", claims=["A method of computing attention."])
    assert _run(q="attention")[0] == "US2B2"


def test_title_outranks_the_same_term_deep_in_the_body(db):
    _doc(db, "US1B2", title="A pump", description="filler " * 300 + "attention")
    _doc(db, "US2B2", title="Attention circuit", description="filler " * 300)
    assert _run(q="attention")[0] == "US2B2"
