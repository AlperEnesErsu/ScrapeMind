"""Plain-language claim readings (Faz 8.6).

The LLM is replaced at `ai_service._call_llm`, so these tests check what goes
into the prompt and what is done with the answer -- never whether a reading is
*faithful* to the claim. That is not testable with a mock, and the design
leans on it not being needed: the original claim is always on the page, the
prompt forbids adding anything the text does not say, and every reading is
labelled as a machine reading.

The load-bearing properties:

- a dependent claim is explained with its chain, independent claim first;
- a reading is cached and shared, and a failed regeneration keeps the old one;
- re-ingesting a corrected grant removes readings of the old text;
- an independent claim never carries a "what it narrows" line.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.modules.patent import explain, ingest, parser
from app.modules.patent.models import PatentClaim, PatentClaimExplanation, PatentDocument
from app.modules.scrape import ai_service


@pytest.fixture(autouse=True)
def clean(db):
    PatentDocument.query.delete()
    db.session.commit()
    yield
    PatentDocument.query.delete()
    db.session.commit()


@pytest.fixture
def llm(monkeypatch):
    """A fake provider that records prompts and returns a scripted answer."""
    state = {"calls": [], "answer": {"plain": "Bir yöntem.", "narrows": "", "terms": []}}

    def fake(*, system, user_msg, max_tokens, user=None, expect_json=True):
        state["calls"].append(user_msg)
        answer = state["answer"]
        return (answer, "{}") if answer is not None else (None, None)

    monkeypatch.setattr(ai_service, "_call_llm", fake)
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_model_label", lambda user=None: "test-model")
    return state


def _doc(db, claims, number="US11123456B2"):
    """`claims` is [(number, text, depends_on)]."""
    doc = PatentDocument(
        doc_number=number,
        country="US",
        title="A patent",
        grant_date=date(2026, 9, 8),
        ai_source="cpc_core",
    )
    db.session.add(doc)
    db.session.flush()
    for n, text, dep in claims:
        db.session.add(
            PatentClaim(
                patent_document_id=doc.id,
                number=n,
                text=text,
                depends_on=dep,
                is_independent=dep is None,
            )
        )
    db.session.commit()
    return doc


def _claim(doc, number):
    return PatentClaim.query.filter_by(patent_document_id=doc.id, number=number).one()


class TestChain:
    def test_independent_first_direct_parent_last(self, db):
        doc = _doc(db, [(1, "root", None), (2, "mid", 1), (3, "leaf", 2)])
        assert [c.number for c in explain.ancestor_chain(_claim(doc, 3))] == [1, 2]

    def test_independent_claim_has_no_chain(self, db):
        doc = _doc(db, [(1, "root", None)])
        assert explain.ancestor_chain(_claim(doc, 1)) == []

    def test_a_missing_parent_ends_the_chain(self, db):
        doc = _doc(db, [(1, "root", None), (3, "orphan", 2)])
        assert explain.ancestor_chain(_claim(doc, 3)) == []

    def test_a_cycle_stops_instead_of_looping(self, db):
        doc = _doc(db, [(1, "a", 2), (2, "b", 1)])
        assert [c.number for c in explain.ancestor_chain(_claim(doc, 1))] == [2]


class TestPrompt:
    def test_the_chain_goes_into_the_prompt_and_the_target_is_marked(self, db, llm):
        doc = _doc(db, [(1, "A method of quantising attention.", None), (2, "wherein 8-bit.", 1)])
        explain.get_or_generate(_claim(doc, 2))
        prompt = llm["calls"][0]
        assert prompt.index("İstem 1 (bağlam)") < prompt.index("İstem 2 (AÇIKLANACAK HEDEF)")
        assert "quantising attention" in prompt and "8-bit" in prompt

    def test_an_independent_claim_never_gets_a_narrows_line(self, db, llm):
        llm["answer"] = {"plain": "Bir yöntem.", "narrows": "invented relationship", "terms": []}
        doc = _doc(db, [(1, "root", None)])
        assert explain.get_or_generate(_claim(doc, 1)).narrows is None

    def test_terms_are_validated_and_capped(self, db, llm):
        llm["answer"] = {
            "plain": "Bir yöntem.",
            "terms": [{"term": f"t{i}", "meaning": "m"} for i in range(10)]
            + [{"term": "no meaning"}, "not a dict"],
        }
        doc = _doc(db, [(1, "root", None)])
        reading = explain.get_or_generate(_claim(doc, 1))
        assert len(reading.terms) == ai_service.CLAIM_EXPLAIN_MAX_TERMS

    def test_an_answer_without_plain_text_is_not_stored(self, db, llm):
        llm["answer"] = {"terms": []}
        doc = _doc(db, [(1, "root", None)])
        assert explain.get_or_generate(_claim(doc, 1)) is None
        assert PatentClaimExplanation.query.count() == 0


class TestCache:
    def test_second_request_does_not_call_the_provider(self, db, llm):
        doc = _doc(db, [(1, "root", None)])
        explain.get_or_generate(_claim(doc, 1))
        explain.get_or_generate(_claim(doc, 1))
        assert len(llm["calls"]) == 1

    def test_force_regenerates(self, db, llm):
        doc = _doc(db, [(1, "root", None)])
        explain.get_or_generate(_claim(doc, 1))
        llm["answer"] = {"plain": "Daha iyi okuma.", "terms": []}
        assert explain.get_or_generate(_claim(doc, 1), force=True).plain == "Daha iyi okuma."
        assert PatentClaimExplanation.query.count() == 1

    def test_a_failed_regeneration_keeps_the_old_reading(self, db, llm):
        doc = _doc(db, [(1, "root", None)])
        explain.get_or_generate(_claim(doc, 1))
        llm["answer"] = None
        assert explain.get_or_generate(_claim(doc, 1), force=True).plain == "Bir yöntem."

    def test_reingesting_a_corrected_grant_removes_readings_of_the_old_text(self, db, llm):
        doc = _doc(db, [(1, "old text", None)])
        explain.get_or_generate(_claim(doc, 1))
        assert PatentClaimExplanation.query.count() == 1
        revised = parser.ParsedPatent(
            doc_number=doc.doc_number,
            kind_code="B2",
            country="US",
            title="A patent",
            abstract=None,
            description=None,
            filing_date=None,
            grant_date=date(2026, 9, 8),
            priority_date=None,
            claims=[parser.ParsedClaim(number=1, text="corrected text", is_independent=True)],
            raw_sha256="changed",
        )
        ingest.upsert(revised, ai_source="cpc_core", source_file="ipg260915.xml")
        db.session.commit()
        assert PatentClaimExplanation.query.count() == 0


class TestRoutes:
    def test_button_then_reading(self, auth_client, db, llm):
        client, _ = auth_client
        doc = _doc(db, [(1, "root", None)])
        body = client.get(f"/patents/{doc.doc_number}").get_data(as_text=True)
        assert "Düz dille açıkla" in body
        assert llm["calls"] == []  # nothing is generated just by opening the page

        body = client.post(f"/patents/{doc.doc_number}/claims/1/explain").get_data(as_text=True)
        assert "Bir yöntem." in body
        assert "hukuki tavsiye değildir" in body

    def test_a_cached_reading_is_shown_on_load(self, auth_client, db, llm):
        client, _ = auth_client
        doc = _doc(db, [(1, "root", None)])
        explain.get_or_generate(_claim(doc, 1))
        calls = len(llm["calls"])
        body = client.get(f"/patents/{doc.doc_number}").get_data(as_text=True)
        assert "Bir yöntem." in body
        assert len(llm["calls"]) == calls

    def test_unknown_claim_is_404(self, auth_client, db, llm):
        client, _ = auth_client
        doc = _doc(db, [(1, "root", None)])
        assert client.post(f"/patents/{doc.doc_number}/claims/9/explain").status_code == 404

    def test_without_ai_nothing_is_called_and_the_page_says_why(
        self, auth_client, db, llm, monkeypatch
    ):
        client, _ = auth_client
        monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)
        doc = _doc(db, [(1, "root", None)])
        body = client.post(f"/patents/{doc.doc_number}/claims/1/explain").get_data(as_text=True)
        assert "AI sağlayıcısının yapılandırılmış olması gerekir" in body
        assert llm["calls"] == []

    def test_a_failed_generation_says_so(self, auth_client, db, llm):
        client, _ = auth_client
        llm["answer"] = None
        doc = _doc(db, [(1, "root", None)])
        body = client.post(f"/patents/{doc.doc_number}/claims/1/explain").get_data(as_text=True)
        assert "Okuma üretilemedi" in body
