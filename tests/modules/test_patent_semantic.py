"""Semantic patent search: claim-1 vectors and RRF fusion (Faz 8.5).

Test embeddings are the deterministic hash vectors from `embedding_service`:
identical text gives an identical vector, and nothing else about them means
anything. So these tests check the plumbing and the arithmetic -- which
vectors exist, which are comparable, how rankings fuse -- never whether a
match is semantically "right". `PATENT_SEMANTIC_MAX_DISTANCE` is set near zero
so only an identical claim counts as a neighbour; otherwise results would
hinge on the accidental geometry of hash vectors.

The load-bearing properties:

- a vector from a different model is never compared with the query;
- a rewritten patent loses its old vector;
- when no query vector can be made the page says so instead of passing full
  text off as semantic results;
- fusion is by rank, and a document both lists found beats one only one did.
"""

from __future__ import annotations

from datetime import date

import pytest
from werkzeug.datastructures import MultiDict

from app.modules.patent import embedding, ingest, parser, search
from app.modules.patent.models import PatentChunk, PatentClaim, PatentDocument

_KEYS = ("PATENT_FTS_ONLY", "EMBEDDING_MODEL", "PATENT_SEMANTIC_MAX_DISTANCE")


@pytest.fixture(autouse=True)
def env(app, db):
    saved = {k: app.config.get(k) for k in _KEYS}
    app.config["PATENT_FTS_ONLY"] = False
    app.config["PATENT_SEMANTIC_MAX_DISTANCE"] = 0.001
    PatentDocument.query.delete()
    db.session.commit()
    yield
    app.config.update(saved)
    PatentDocument.query.delete()
    db.session.commit()


def _doc(db, number, *, claim1="A method.", description="", cpc=("G06N3/08",), claims=None):
    doc = PatentDocument(
        doc_number=number,
        country="US",
        title="A patent",
        description=description,
        cpc_codes=list(cpc),
        assignees=["Example AI Inc."],
        grant_date=date(2026, 9, 8),
        ai_source="cpc_core",
    )
    db.session.add(doc)
    db.session.flush()
    for i, text in enumerate(claims or [claim1], start=1):
        db.session.add(
            PatentClaim(patent_document_id=doc.id, number=i, is_independent=i == 1, text=text)
        )
    db.session.commit()
    return doc


def _filters(**args):
    return search.SearchFilters.from_args(MultiDict(args))


class TestFuse:
    def test_found_by_both_beats_found_by_one(self):
        assert search.fuse([1, 2, 3], [3, 4])[0] == 3

    def test_rank_not_score_decides(self):
        # 2 is second in both lists; 1 and 4 are first in only one each.
        assert search.fuse([1, 2], [4, 2])[0] == 2

    def test_ties_break_on_first_appearance(self):
        assert search.fuse([10], [20]) == [10, 20]

    def test_empty_lists(self):
        assert search.fuse([], []) == []


class TestListPagination:
    def test_counts(self):
        p = search.ListPagination(items=[], page=2, per_page=20, total=45)
        assert (p.pages, p.has_prev, p.has_next, p.prev_num, p.next_num) == (3, True, True, 1, 3)

    def test_gaps_are_none(self):
        p = search.ListPagination(items=[], page=10, per_page=1, total=20)
        pages = list(p.iter_pages(left_edge=1, right_edge=1, left_current=2, right_current=2))
        assert pages == [1, None, 8, 9, 10, 11, 12, None, 20]

    def test_nothing(self):
        p = search.ListPagination(items=[], page=1, per_page=20, total=0)
        assert p.pages == 0 and not p.has_next


class TestEmbedPending:
    def test_embeds_claim_one_and_records_the_model(self, db):
        doc = _doc(db, "US1B2", claims=["First claim text.", "The method of claim 1."])
        result = embedding.embed_pending()
        assert result["embedded"] == 1
        chunk = PatentChunk.query.filter_by(patent_document_id=doc.id).one()
        assert chunk.kind == "claim1" and chunk.ref == "1"
        assert chunk.text == "First claim text."
        assert chunk.embedding_model == embedding.current_model()

    def test_second_run_does_nothing(self, db):
        _doc(db, "US1B2")
        embedding.embed_pending()
        assert embedding.embed_pending()["embedded"] == 0

    def test_a_model_change_makes_every_vector_stale(self, app, db):
        doc = _doc(db, "US1B2")
        embedding.embed_pending()
        app.config["EMBEDDING_MODEL"] = "some-other-model"
        assert embedding.coverage() == (0, 1)
        assert embedding.embed_pending()["embedded"] == 1
        chunk = PatentChunk.query.filter_by(patent_document_id=doc.id).one()
        assert chunk.embedding_model.startswith("some-other-model@")

    def test_full_text_only_skips(self, app, db):
        _doc(db, "US1B2")
        app.config["PATENT_FTS_ONLY"] = True
        assert embedding.embed_pending()["status"] == "skipped"
        assert PatentChunk.query.count() == 0

    def test_a_failed_provider_call_leaves_the_document_pending(self, db, monkeypatch):
        _doc(db, "US1B2")
        monkeypatch.setattr(
            "app.modules.scrape.embedding_service.get_embeddings_batch",
            lambda texts, user=None: [None] * len(texts),
        )
        result = embedding.embed_pending()
        assert result["status"] == "partial" and result["failed"] == 1
        assert embedding.coverage() == (0, 1)

    def test_a_rewritten_patent_loses_its_old_vector(self, db):
        doc = _doc(db, "US11123456B2")
        embedding.embed_pending()
        assert PatentChunk.query.count() == 1
        revised = parser.ParsedPatent(
            doc_number="US11123456B2",
            kind_code="B2",
            country="US",
            title="A patent",
            abstract=None,
            description=None,
            filing_date=None,
            grant_date=date(2026, 9, 8),
            priority_date=None,
            claims=[parser.ParsedClaim(number=1, text="A corrected claim.", is_independent=True)],
            raw_sha256="changed",
        )
        ingest.upsert(revised, ai_source="cpc_core", source_file="ipg260915.xml")
        db.session.commit()
        assert PatentChunk.query.filter_by(patent_document_id=doc.id).count() == 0


class TestHybrid:
    def test_vector_hits_are_fused_with_text_hits(self, db):
        """Both documents come back, and B carries a similarity score.

        Not a test that meaning finds a patent sharing no words with the query:
        hash vectors can only match identical text, so B's claim has to repeat
        the query and is a text hit too. That property belongs to the real
        embedding model and cannot be shown with mocks; what this shows is that
        vector candidates reach the fused list with their similarity attached."""
        _doc(db, "US1B2", description="zeppelin mooring mast")
        b = _doc(db, "US2B2", claim1="zeppelin")
        embedding.embed_pending()
        result = search.hybrid_search(_filters(q="zeppelin", semantic="1"), 1)
        ids = [d.doc_number for d in result.pagination.items]
        assert result.semantic_used
        assert set(ids) == {"US1B2", "US2B2"}
        assert result.similarity[b.id] == pytest.approx(1.0)

    def test_found_by_both_ranks_first(self, db):
        _doc(db, "US1B2", description="zeppelin")
        _doc(db, "US2B2", claim1="zeppelin")  # text match in claims AND vector match
        embedding.embed_pending()
        result = search.hybrid_search(_filters(q="zeppelin", semantic="1"), 1)
        assert result.pagination.items[0].doc_number == "US2B2"

    def test_structured_filters_apply_to_vector_candidates(self, db):
        _doc(db, "US1B2", claim1="zeppelin", cpc=["G06V10/82"])
        embedding.embed_pending()
        result = search.hybrid_search(_filters(q="zeppelin", semantic="1", cpc="G06N"), 1)
        assert result.pagination.items == []

    def test_vectors_from_another_model_are_ignored(self, app, db):
        _doc(db, "US1B2", claim1="zeppelin mast")
        embedding.embed_pending()
        app.config["EMBEDDING_MODEL"] = "some-other-model"
        # "zeppelin mast" is not a full-text match for the query below either,
        # so anything returned would have come from a stale vector.
        result = search.hybrid_search(_filters(q="zeppelin mast", semantic="1", scope="claims"), 1)
        assert result.similarity == {}

    def test_no_query_vector_falls_back_and_says_so(self, db, monkeypatch):
        _doc(db, "US1B2", description="zeppelin")
        monkeypatch.setattr(
            "app.modules.scrape.embedding_service.get_embedding", lambda q, user=None: None
        )
        result = search.hybrid_search(_filters(q="zeppelin", semantic="1"), 1)
        assert not result.semantic_used
        assert [d.doc_number for d in result.pagination.items] == ["US1B2"]


class TestPage:
    def test_semantic_hit_shows_similarity_and_the_matched_claim(self, auth_client, db):
        client, _ = auth_client
        _doc(db, "US2B2", claim1="zeppelin")
        embedding.embed_pending()
        body = client.get("/patents/search?q=zeppelin&semantic=1").get_data(as_text=True)
        assert "%100 benzer" in body
        assert "zeppelin" in body

    def test_unavailable_semantic_is_announced(self, auth_client, db, monkeypatch):
        client, _ = auth_client
        _doc(db, "US1B2", description="zeppelin")
        monkeypatch.setattr(
            "app.modules.scrape.embedding_service.get_embedding", lambda q, user=None: None
        )
        body = client.get("/patents/search?q=zeppelin&semantic=1").get_data(as_text=True)
        assert "yalnızca tam metin aramasından geliyor" in body


def test_embed_task_is_routed_to_the_billable_pool():
    from app.tasks import TASK_ROUTES
    from app.tasks.schedule import BEAT_SCHEDULE

    assert TASK_ROUTES["patents_bulk.embed_pending"] == {"queue": "llm"}
    assert any(e["task"] == "patents_bulk.embed_pending" for e in BEAT_SCHEDULE.values())


def test_a_load_queues_embedding_after_it(app, monkeypatch):
    from app.tasks import patent_bulk_tasks

    queued = []
    monkeypatch.setattr("app.modules.patent.ingest.refresh_window", lambda limit=None: {"ok": 1})
    monkeypatch.setattr(patent_bulk_tasks.embed_pending, "delay", lambda: queued.append(1))
    monkeypatch.setattr(patent_bulk_tasks, "_lock", lambda: None)
    with app.app_context():
        patent_bulk_tasks.refresh_window.run()
    assert queued == [1]


def test_a_capped_hybrid_total_is_not_presented_as_the_match_count(auth_client, db, monkeypatch):
    """Fusion sees only the top candidates of each list; the page must not
    tell someone searching a common term that only that many patents match."""
    client, _ = auth_client
    monkeypatch.setattr(search, "CANDIDATES", 3)
    for i in range(5):
        _doc(db, f"US{i}B2", description="zeppelin")
    result = search.hybrid_search(_filters(q="zeppelin", semantic="1"), 1)
    assert result.capped and result.text_total == 5 and result.pagination.total == 3
    body = client.get("/patents/search?q=zeppelin&semantic=1").get_data(as_text=True)
    assert "5 tam metin eşleşmesinin en alakalı 3 tanesi" in body
