"""Tests for the embedding service (Faz 5.4 pgvector integration)."""

from __future__ import annotations

import math

import pytest

from app.core.models.user import User  # noqa: F401
from app.modules.scrape.embedding_service import (
    deterministic_mock_embedding,
    embed_paper,
    embed_papers_batch,
    get_embedding,
    get_embeddings_batch,
    is_embedding_enabled,
    paper_text_for_embedding,
)
from app.modules.scrape.models import Paper


def test_paper_text_for_embedding(app):
    with app.app_context():
        p1 = Paper(
            title="Attention Is All You Need",
            abstract="The dominant sequence transduction models...",
        )
        text1 = paper_text_for_embedding(p1)
        assert "Attention Is All You Need" in text1
        assert "The dominant sequence transduction models..." in text1

        p2 = Paper(title="Solo Title", abstract=None)
        assert paper_text_for_embedding(p2) == "Solo Title"

        p3 = Paper(title="", abstract="")
        assert paper_text_for_embedding(p3) == ""


def test_deterministic_mock_embedding():
    vec1 = deterministic_mock_embedding("machine learning", dim=1536)
    vec2 = deterministic_mock_embedding("machine learning", dim=1536)
    vec3 = deterministic_mock_embedding("deep learning", dim=1536)

    assert len(vec1) == 1536
    # Deterministic: same text yields identical vectors
    assert vec1 == vec2
    # Different text yields different vectors
    assert vec1 != vec3

    # Unit norm check: sum(x^2) ~= 1.0
    norm = math.sqrt(sum(x * x for x in vec1))
    assert pytest.approx(norm, rel=1e-5) == 1.0


def test_get_embedding_and_batch(app):
    with app.app_context():
        vec = get_embedding("transformers and self-attention")
        assert vec is not None
        assert len(vec) == 1536

        batch = get_embeddings_batch(["paper one", "paper two", ""])
        assert len(batch) == 3
        assert batch[0] is not None
        assert batch[1] is not None
        assert batch[2] is None  # empty text returns None


def test_embed_paper_and_batch_in_db(app, db):
    with app.app_context():
        p1 = Paper(
            source="arxiv",
            external_id="2409.embed001",
            title="Graph Neural Networks for Chemistry",
            abstract="We present a novel GNN architecture for molecular property prediction.",
        )
        p2 = Paper(
            source="arxiv",
            external_id="2409.embed002",
            title="Quantum Machine Learning Algorithms",
            abstract="Quantum variational circuits applied to classification.",
        )
        db.session.add_all([p1, p2])
        db.session.commit()

        assert p1.embedding is None
        assert p2.embedding is None

        # Embed single paper
        ok = embed_paper(p1, commit=True)
        assert ok is True
        assert p1.embedding is not None
        assert len(p1.embedding) == 1536

        # Embed batch
        count = embed_papers_batch([p1, p2], commit=True)
        assert count == 1  # p1 already embedded, only p2 embedded
        assert p2.embedding is not None
        assert len(p2.embedding) == 1536

        # Teardown
        db.session.delete(p1)
        db.session.delete(p2)
        db.session.commit()


def test_is_embedding_enabled(app):
    with app.app_context():
        # In test mode with MOCK_EMBEDDINGS=True (default in tests)
        assert is_embedding_enabled() is True

        # When provider is explicitly none
        app.config["EMBEDDING_PROVIDER"] = "none"
        assert is_embedding_enabled() is False

        # When mock embeddings is turned off and no key is configured
        app.config["EMBEDDING_PROVIDER"] = "openrouter"
        app.config["MOCK_EMBEDDINGS"] = False
        assert is_embedding_enabled() is False

        # When an API key is provided
        app.config["OPENROUTER_API_KEY"] = "sk-or-test"
        assert is_embedding_enabled() is True

        # Reset config
        app.config["EMBEDDING_PROVIDER"] = ""
        app.config["OPENROUTER_API_KEY"] = ""
        app.config["MOCK_EMBEDDINGS"] = True
