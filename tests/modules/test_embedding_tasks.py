"""Tests for embedding Celery tasks (Faz 5.4)."""

from __future__ import annotations

from app.core.models.user import User  # noqa: F401
from app.modules.scrape.models import Paper
from app.tasks.embedding_tasks import embed_paper_task, embed_pending_papers_task


def test_embed_paper_task(app, db, monkeypatch):
    with app.app_context():
        p = Paper(
            source="arxiv",
            external_id="2409.task_embed1",
            title="Vision Transformers for Object Detection",
            abstract="Novel ViT architecture for real-time detection.",
        )
        db.session.add(p)
        db.session.commit()

        # When embedding is disabled
        monkeypatch.setattr("app.tasks.embedding_tasks.is_embedding_enabled", lambda: False)
        assert embed_paper_task(p.id) is False

        # When embedding is enabled
        monkeypatch.setattr("app.tasks.embedding_tasks.is_embedding_enabled", lambda: True)
        res = embed_paper_task(p.id)
        assert res is True

        db.session.refresh(p)
        assert p.embedding is not None
        assert len(p.embedding) == 1536

        # Second run is a no-op because it already has an embedding
        assert embed_paper_task(p.id) is False

        # Cleanup
        db.session.delete(p)
        db.session.commit()


def test_embed_pending_papers_task(app, db):
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = "sk-test"
        p1 = Paper(
            source="arxiv",
            external_id="2409.batch_task1",
            title="Reinforcement Learning with Human Feedback",
            abstract="RLHF methodology.",
        )
        p2 = Paper(
            source="arxiv",
            external_id="2409.batch_task2",
            title="Direct Preference Optimization",
            abstract="DPO as an alternative to RLHF.",
        )
        db.session.add_all([p1, p2])
        db.session.commit()

        count = embed_pending_papers_task(limit=10)
        assert count >= 2

        db.session.refresh(p1)
        db.session.refresh(p2)
        assert p1.embedding is not None
        assert p2.embedding is not None

        # Cleanup
        db.session.delete(p1)
        db.session.delete(p2)
        db.session.commit()
        app.config["OPENROUTER_API_KEY"] = ""
