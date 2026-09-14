"""Paper vectors record their model; library search escapes LIKE and reads full text.

Review items T4 and T5, plus a bug found while doing them.

- **T4.** A vector from one embedding model compared with a vector from
  another returns a distance that looks exactly like a real one. Every semantic
  read -- library search, the feed, "similar papers", RAG chat -- must compare
  only same-model vectors, and the pending task must redo the rest.
- **T5.** `%` or `_` typed into a search box must match literally.
- **Found:** `search_user_papers_query` has promised full-text matching since
  Faz 7.0 and never did it. Library search and saved-search alerts could not
  see a term that lived only in a paper's stored full text.

Test embeddings are deterministic hash vectors: identical text gives an
identical vector (distance 0). The query text below never appears in titles or
abstracts, so any semantic hit can only have come from a vector.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.modules.scrape import alerts
from app.modules.scrape.embedding_service import (
    current_embedding_model,
    deterministic_mock_embedding,
    embed_paper,
    needs_embedding,
)
from app.modules.scrape.models import Paper, SavedSearch, UserPaper
from app.modules.scrape.service import list_user_papers, search_user_papers_query

QUERY = "zeppelinmast"
OTHER_MODEL = "some-retired-model@1536"


@pytest.fixture
def user(db):
    from app.core.models.user import User

    suffix = uuid.uuid4().hex[:8]
    u = User(
        username=f"embmodel-{suffix}",
        email=f"embmodel-{suffix}@example.test",
        full_name="Embedding model tester",
        password_hash="x",
        locale="tr",
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def add(db, user):
    def _add(
        *,
        title="An unrelated title",
        abstract="Nothing to see.",
        fulltext=None,
        vector_text=None,
        model="current",
        categories=None,
        matched_keyword=None,
    ) -> tuple[Paper, UserPaper]:
        paper = Paper(
            source="openalex",
            external_id=f"W{uuid.uuid4().hex[:12]}",
            title=title,
            abstract=abstract,
            fulltext=fulltext,
            categories=categories,
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        if vector_text is not None:
            paper.embedding = deterministic_mock_embedding(vector_text)
            paper.embedding_model = current_embedding_model() if model == "current" else model
        db.session.add(paper)
        db.session.flush()
        link = UserPaper(user_id=user.id, paper_id=paper.id, matched_keyword=matched_keyword)
        db.session.add(link)
        db.session.commit()
        return paper, link

    return _add


def _library_ids(user, **kwargs) -> set[int]:
    return {up.paper_id for up in search_user_papers_query(user, **kwargs).all()}


def _feed_ids(user, **kwargs) -> set[int]:
    return {up.paper_id for up in list_user_papers(user, view="timeline", **kwargs)}


# ---------------------------------------------------------------- T4 writes


class TestStamp:
    def test_embedding_a_paper_records_the_model(self, db, add):
        paper, _ = add(title="Stamp me")
        assert embed_paper(paper)
        assert paper.embedding_model == current_embedding_model()

    def test_needs_embedding(self, db, add):
        missing, _ = add()
        current, _ = add(vector_text="x")
        stale, _ = add(vector_text="x", model=OTHER_MODEL)
        unstamped, _ = add(vector_text="x", model=None)
        assert needs_embedding(missing)
        assert not needs_embedding(current)
        assert needs_embedding(stale)
        assert needs_embedding(unstamped)

    def test_the_pending_task_redoes_stale_vectors(self, app, db, add):
        from app.tasks.embedding_tasks import embed_pending_papers_task

        stale, _ = add(title="Stale one", vector_text="x", model=OTHER_MODEL)
        with app.app_context():
            embed_pending_papers_task.run(limit=500)
        db.session.refresh(stale)
        assert stale.embedding_model == current_embedding_model()


# ----------------------------------------------------------------- T4 reads


class TestComparableOnly:
    def test_library_search_ignores_other_model_vectors(self, db, user, add):
        same, _ = add(vector_text=QUERY)
        other, _ = add(vector_text=QUERY, model=OTHER_MODEL)
        ids = _library_ids(user, q=QUERY, semantic=True)
        assert same.id in ids
        assert other.id not in ids

    def test_feed_search_ignores_other_model_vectors(self, db, user, add):
        same, _ = add(vector_text=QUERY)
        other, _ = add(vector_text=QUERY, model=OTHER_MODEL)
        ids = _feed_ids(user, q=QUERY, semantic=True)
        assert same.id in ids
        assert other.id not in ids

    def test_similar_papers_compare_only_same_model(self, db, add):
        from app.modules.scrape.routes import _get_internal_similar

        _main_paper, main_link = add(vector_text="shared")
        _same, same_link = add(vector_text="shared")
        _other, other_link = add(vector_text="shared", model=OTHER_MODEL)
        ids = {up.id for up in _get_internal_similar(main_link, limit=10)}
        assert same_link.id in ids
        assert other_link.id not in ids

    def test_rag_context_skips_other_model_papers(self, app, db, user, add, monkeypatch):
        from app.modules.scrape import ai_service

        target, _ = add(title="Target paper", vector_text="target")
        add(title="Same model neighbour", vector_text=QUERY)
        add(title="Retired model neighbour", vector_text=QUERY, model=OTHER_MODEL)

        captured = {}

        def fake_llm(*, system, user_msg, max_tokens, user=None, expect_json=False):
            captured["system"] = system
            return "ok", "{}"

        monkeypatch.setattr(ai_service, "is_ai_enabled", lambda u: True)
        monkeypatch.setattr(ai_service, "_call_llm", fake_llm)
        with app.app_context():
            ai_service.ask_paper(target, QUERY, user=user)
        assert "Same model neighbour" in captured["system"]
        assert "Retired model neighbour" not in captured["system"]


# ---------------------------------------------------------------------- T5


class TestLikeEscaping:
    @pytest.mark.parametrize("search", [_library_ids, _feed_ids])
    def test_percent_is_literal(self, db, user, add, search):
        hit, _ = add(title="A 50% faster sampler")
        miss, _ = add(title="A 50 times faster sampler")
        ids = search(user, q="50%")
        assert hit.id in ids and miss.id not in ids

    @pytest.mark.parametrize("search", [_library_ids, _feed_ids])
    def test_underscore_is_literal(self, db, user, add, search):
        hit, _ = add(title="Parsing snake_case identifiers")
        miss, _ = add(title="Parsing snakeXcase identifiers")
        ids = search(user, q="snake_case")
        assert hit.id in ids and miss.id not in ids

    def test_a_lone_wildcard_does_not_match_everything(self, db, user, add):
        add(title="Alpha")
        add(title="Beta")
        assert _library_ids(user, q="%") == set()


# -------------------------------------------------------------- full text


class TestLibraryFullText:
    def test_a_term_only_in_full_text_is_found(self, db, user, add):
        """The promise in `search_user_papers_query`'s docstring since Faz 7.0."""
        paper, _ = add(fulltext=f"deep in section 4 we use {QUERY} for mooring")
        assert paper.id in _library_ids(user, q=QUERY)

    def test_a_saved_search_alert_fires_when_full_text_arrives(self, db, user, add):
        """The case migration e7b204c9f83a says alerts exist for: the paper is
        old, nothing about it is new, and the term turns up in its body."""
        paper, _ = add()
        search = SavedSearch(user_id=user.id, name="watch", q=QUERY)
        db.session.add(search)
        db.session.commit()
        assert alerts.find_new_matches(search).count == 0

        paper.fulltext = f"the method relies on {QUERY}"
        db.session.commit()
        result = alerts.find_new_matches(search)
        assert [p.id for p in result.papers] == [paper.id]
