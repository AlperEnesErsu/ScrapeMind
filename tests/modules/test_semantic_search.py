"""Tests for semantic search and pgvector-based similarity ranking (Faz 5.4)."""

from __future__ import annotations

import pytest

from app.core.models.user import User
from app.modules.scrape.embedding_service import deterministic_mock_embedding
from app.modules.scrape.models import Paper, UserPaper
from app.modules.scrape.routes import _get_internal_similar
from app.modules.scrape.service import search_user_papers_query


@pytest.fixture
def semantic_user(db):
    """Clean test user for semantic search tests."""
    user = User.query.filter_by(username="semantictester").first()
    if user:
        # Clean up related rows
        for up in UserPaper.query.filter_by(user_id=user.id).all():
            db.session.delete(up)
        db.session.commit()
        db.session.delete(user)
        db.session.commit()

    from app.core.auth.strategies.local import LocalAuthStrategy

    user = User(
        username="semantictester",
        email="semantictester@example.test",
        full_name="Semantic Tester",
        password_hash=LocalAuthStrategy.hash_password("Password123!"),
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()

    yield user

    # Teardown
    for up in UserPaper.query.filter_by(user_id=user.id).all():
        db.session.delete(up)
    db.session.delete(user)
    db.session.commit()


def test_internal_similar_uses_pgvector_cosine_distance(app, db, semantic_user):
    with app.app_context():
        # Create papers with specific embeddings
        p_target = Paper(
            source="arxiv",
            external_id="2409.target",
            title="Transformer Models in Natural Language",
            abstract="Deep attention architectures.",
            embedding=deterministic_mock_embedding("transformer attention language"),
        )
        # Very close semantic neighbor
        p_close = Paper(
            source="arxiv",
            external_id="2409.close",
            title="Self-Attention for Language Understanding",
            abstract="Attention mechanisms in NLP.",
            embedding=deterministic_mock_embedding("transformer attention language"),
        )
        # Distant paper
        p_distant = Paper(
            source="arxiv",
            external_id="2409.distant",
            title="Marine Biology in the Pacific Ocean",
            abstract="Study of deep sea corals and marine life.",
            embedding=deterministic_mock_embedding("marine biology ocean coral"),
        )

        db.session.add_all([p_target, p_close, p_distant])
        db.session.commit()

        up_target = UserPaper(user_id=semantic_user.id, paper_id=p_target.id)
        up_close = UserPaper(user_id=semantic_user.id, paper_id=p_close.id)
        up_distant = UserPaper(user_id=semantic_user.id, paper_id=p_distant.id)
        db.session.add_all([up_target, up_close, up_distant])
        db.session.commit()

        similar = _get_internal_similar(up_target, limit=2)
        assert len(similar) >= 1
        # The closest paper should be first
        assert similar[0].paper_id == p_close.id
        assert hasattr(similar[0], "similarity_score")
        assert similar[0].similarity_score is not None
        assert similar[0].similarity_score > 0.5

        # Cleanup papers
        db.session.delete(up_target)
        db.session.delete(up_close)
        db.session.delete(up_distant)
        db.session.delete(p_target)
        db.session.delete(p_close)
        db.session.delete(p_distant)
        db.session.commit()


def test_internal_similar_fallback_when_no_embeddings(app, db, semantic_user):
    with app.app_context():
        p_main = Paper(
            source="arxiv",
            external_id="2409.noembed1",
            title="Astrophysics and Supernovae",
            abstract="Stellar evolution.",
            embedding=None,
        )
        p_kw_match = Paper(
            source="arxiv",
            external_id="2409.noembed2",
            title="Galaxy Formation",
            abstract="Cosmology models.",
            embedding=None,
        )
        db.session.add_all([p_main, p_kw_match])
        db.session.commit()

        up_main = UserPaper(
            user_id=semantic_user.id, paper_id=p_main.id, matched_keyword="astronomy"
        )
        up_kw = UserPaper(
            user_id=semantic_user.id, paper_id=p_kw_match.id, matched_keyword="astronomy"
        )
        db.session.add_all([up_main, up_kw])
        db.session.commit()

        # Should fall back cleanly to keyword match
        similar = _get_internal_similar(up_main, limit=2)
        assert len(similar) == 1
        assert similar[0].paper_id == p_kw_match.id
        # Fallback doesn't attach similarity_score
        assert getattr(similar[0], "similarity_score", None) is None

        # Cleanup
        db.session.delete(up_main)
        db.session.delete(up_kw)
        db.session.delete(p_main)
        db.session.delete(p_kw_match)
        db.session.commit()


def test_search_user_papers_query_semantic(app, db, semantic_user):
    with app.app_context():
        p1 = Paper(
            source="arxiv",
            external_id="2409.sem1",
            title="Deep Convolutional Networks",
            abstract="Image recognition with CNNs.",
            embedding=deterministic_mock_embedding("computer vision convolutional networks"),
        )
        p2 = Paper(
            source="arxiv",
            external_id="2409.sem2",
            title="Ancient Roman Architecture",
            abstract="Building techniques in antiquity.",
            embedding=deterministic_mock_embedding("history ancient rome architecture"),
        )
        db.session.add_all([p1, p2])
        db.session.commit()

        up1 = UserPaper(user_id=semantic_user.id, paper_id=p1.id)
        up2 = UserPaper(user_id=semantic_user.id, paper_id=p2.id)
        db.session.add_all([up1, up2])
        db.session.commit()

        # 1. Semantic search for vision should rank p1 first
        q_sem = search_user_papers_query(
            semantic_user,
            q="computer vision convolutional networks",
            semantic=True,
        )
        results_sem = q_sem.all()
        assert len(results_sem) >= 1
        assert results_sem[0].paper_id == p1.id

        # 2. Text LIKE search for "Ancient"
        q_text = search_user_papers_query(semantic_user, q="Ancient", semantic=False)
        results_text = q_text.all()
        assert len(results_text) == 1
        assert results_text[0].paper_id == p2.id

        # Cleanup
        db.session.delete(up1)
        db.session.delete(up2)
        db.session.delete(p1)
        db.session.delete(p2)
        db.session.commit()


def test_library_search_semantic_route(auth_client):
    client, _user_id = auth_client
    resp = client.get("/library/search?q=neural&semantic=1")
    assert resp.status_code == 200
    assert b"semantic" in resp.data.lower()


def test_feed_semantic_route(auth_client):
    client, _user_id = auth_client
    resp = client.get("/?q=neural&semantic=1")
    assert resp.status_code == 200
    assert b"semantic" in resp.data.lower()
