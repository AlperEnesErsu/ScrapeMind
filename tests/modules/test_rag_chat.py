"""Tests for True RAG Chat in ask_paper (Faz 5.4)."""

from __future__ import annotations

import pytest

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape import ai_service
from app.modules.scrape.embedding_service import deterministic_mock_embedding
from app.modules.scrape.models import Paper, PaperAnalysis, PaperNote, UserPaper


@pytest.fixture
def rag_user(db):
    """Clean test user for RAG chat tests."""
    user = User.query.filter_by(username="ragtester").first()
    if user:
        for up in UserPaper.query.filter_by(user_id=user.id).all():
            db.session.delete(up)
        db.session.delete(user)
        db.session.commit()

    user = User(
        username="ragtester",
        email="ragtester@example.test",
        full_name="RAG Tester",
        password_hash=LocalAuthStrategy.hash_password("Password123!"),
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()

    yield user

    for up in UserPaper.query.filter_by(user_id=user.id).all():
        db.session.delete(up)
    db.session.delete(user)
    db.session.commit()


def test_ask_paper_full_rag_context(app, db, rag_user, monkeypatch):
    with app.app_context():
        # Setup target paper
        p_target = Paper(
            source="arxiv",
            external_id="2409.rag_target",
            title="Efficient Attention for Long Documents",
            abstract="We present linear attention mechanisms that scale to 100k tokens.",
            embedding=deterministic_mock_embedding("efficient attention linear transformer"),
        )
        # Setup another paper in the library that is relevant to the question
        p_related = Paper(
            source="arxiv",
            external_id="2409.rag_related",
            title="State Space Models for Sequential Data",
            abstract="Mamba architecture alternative to transformers with sub-quadratic compute.",
            embedding=deterministic_mock_embedding("state space models mamba architecture"),
        )
        db.session.add_all([p_target, p_related])
        db.session.commit()

        up_target = UserPaper(user_id=rag_user.id, paper_id=p_target.id)
        up_related = UserPaper(user_id=rag_user.id, paper_id=p_related.id)
        db.session.add_all([up_target, up_related])
        db.session.commit()

        # Add structured analysis
        analysis = PaperAnalysis(
            paper_id=p_target.id,
            target_lang="tr",
            tldr="100k token bağlamı için doğrusal dikkat mimarisi.",
            method=["Doğrusal kernel yaklaşımı", "Blok tabanlı bellek optimizasyonu"],
            findings=["Bellek tüketimi %70 azaldı", "Doğruluk standard transformer ile başa baş"],
            limitations=["Eğitim süresi %15 daha uzun"],
        )
        db.session.add(analysis)

        # Add user note
        note = PaperNote(
            user_paper_id=up_target.id,
            tag="Kritik",
            body="Bu yaklaşımı kendi modelimize entegre edebiliriz.",
        )
        db.session.add(note)
        db.session.commit()

        captured_calls = []

        def fake_call_llm(*, system, user_msg, max_tokens, user=None, expect_json=False):
            captured_calls.append({"system": system, "user_msg": user_msg})
            return "Bu makale doğrusal dikkat kullanarak verimlilik sağlıyor.", "{}"

        monkeypatch.setattr(ai_service, "is_ai_enabled", lambda u: True)
        monkeypatch.setattr(ai_service, "_call_llm", fake_call_llm)

        question = "state space models mamba architecture"
        answer = ai_service.ask_paper(p_target, question, user=rag_user)

        assert answer is not None
        assert len(captured_calls) == 1

        sys_prompt = captured_calls[0]["system"]
        # 1. Target paper title & abstract
        assert "Efficient Attention for Long Documents" in sys_prompt
        assert "100k tokens" in sys_prompt

        # 2. Structured analysis
        assert "YAPILANDIRILMIŞ MAKALE ANALİZİ" in sys_prompt
        assert "Doğrusal kernel yaklaşımı" in sys_prompt
        assert "Bellek tüketimi %70 azaldı" in sys_prompt

        # 3. User notes
        assert "KULLANICININ BU MAKALE ÜZERİNE NOTLARI" in sys_prompt
        assert "Bu yaklaşımı kendi modelimize entegre edebiliriz" in sys_prompt

        # 4. Cross-paper library RAG retrieval
        assert "KÜTÜPHANESİNDEN ÇEKİLEN İLGİLİ DİĞER ÇALIŞMALAR (RAG BAĞLAMI)" in sys_prompt
        assert "State Space Models for Sequential Data" in sys_prompt

        # Cleanup
        db.session.delete(analysis)
        db.session.delete(up_target)
        db.session.delete(up_related)
        db.session.delete(p_target)
        db.session.delete(p_related)
        db.session.commit()


def test_ask_paper_disabled_ai(app, rag_user, monkeypatch):
    with app.app_context():
        p = Paper(
            source="arxiv",
            external_id="2409.rag_dis",
            title="Some Paper",
            abstract="Some abstract",
        )
        monkeypatch.setattr(ai_service, "is_ai_enabled", lambda u: False)
        assert ai_service.ask_paper(p, "What is this?", user=rag_user) is None
