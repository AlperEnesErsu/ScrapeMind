"""Unit tests for the Claude AI service layer.

Claude is never called for real — _call_claude / is_ai_enabled are
monkeypatched. These lock in the cache-first behaviour, the JSON parsing
helpers, and the "never poison the cache on failure" contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.extensions import db as _db
from app.modules.scrape import ai_service
from app.modules.scrape.models import Paper, PaperAnalysis, PaperTranslation

# --------------------------------------------------------------------------
# Pure helpers — no DB, no Claude
# --------------------------------------------------------------------------


def test_strip_code_fence_plain():
    assert ai_service._strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_strip_code_fence_wrapped():
    fenced = '```json\n{"a": 1}\n```'
    assert ai_service._strip_code_fence(fenced) == '{"a": 1}'


def test_safe_str():
    assert ai_service._safe_str("  hi  ") == "hi"
    assert ai_service._safe_str("") is None
    assert ai_service._safe_str(None) is None


def test_safe_list():
    assert ai_service._safe_list(["a", " b ", ""]) == ["a", "b"]
    assert ai_service._safe_list([]) is None
    assert ai_service._safe_list(None) is None
    assert ai_service._safe_list("solo") == ["solo"]


# --------------------------------------------------------------------------
# Analysis + translation cache behaviour
# --------------------------------------------------------------------------


@pytest.fixture
def a_paper(db):
    for tbl in ("paper_analyses", "paper_translations", "user_papers", "papers"):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.commit()
    p = Paper(
        source="arxiv",
        external_id="2401.ai001",
        title="A Paper",
        abstract="An abstract about transformers.",
        authors=["A. One"],
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        categories=["cs.LG"],
    )
    db.session.add(p)
    db.session.commit()
    yield p
    for tbl in ("paper_analyses", "paper_translations", "papers"):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.commit()


_FAKE_ANALYSIS = {
    "tldr": "kısa özet",
    "method": ["yöntem 1", "yöntem 2"],
    "findings": ["bulgu 1"],
    "limitations": ["kısıt 1"],
    "personal_relevance": "ilgi alanına uygun",
}


def test_generate_analysis_persists(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda: True)
    monkeypatch.setattr(ai_service, "_call_claude", lambda **kw: (_FAKE_ANALYSIS, "{}"))
    with app.app_context():
        result = ai_service.generate_analysis(a_paper)
        assert result is not None
        assert result.tldr == "kısa özet"
        assert result.method == ["yöntem 1", "yöntem 2"]
        # Cached row is now present.
        assert ai_service.get_analysis(a_paper) is not None
        assert PaperAnalysis.query.filter_by(paper_id=a_paper.id).count() == 1


def test_generate_analysis_disabled_returns_none(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda: False)
    with app.app_context():
        assert ai_service.generate_analysis(a_paper) is None
        assert PaperAnalysis.query.filter_by(paper_id=a_paper.id).count() == 0


def test_generate_analysis_claude_failure_no_cache(app, a_paper, monkeypatch):
    """A failed Claude call returns None and must not persist a partial row."""
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda: True)
    monkeypatch.setattr(ai_service, "_call_claude", lambda **kw: (None, None))
    with app.app_context():
        assert ai_service.generate_analysis(a_paper) is None
        assert PaperAnalysis.query.filter_by(paper_id=a_paper.id).count() == 0


def test_get_or_generate_analysis_cache_hit_skips_claude(app, a_paper, monkeypatch):
    """When a row already exists, Claude must not be called again."""
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda: True)
    with app.app_context():
        _db.session.add(PaperAnalysis(paper_id=a_paper.id, target_lang="tr", tldr="mevcut"))
        _db.session.commit()

        def _boom(**kw):
            raise AssertionError("Claude should not be called on a cache hit")

        monkeypatch.setattr(ai_service, "_call_claude", _boom)
        result = ai_service.get_or_generate_analysis(a_paper)
        assert result.tldr == "mevcut"


def test_generate_translation_persists(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda: True)
    monkeypatch.setattr(
        ai_service,
        "_call_claude",
        lambda **kw: ({"title_translated": "Bir Makale", "abstract_translated": "Bir özet."}, "{}"),
    )
    with app.app_context():
        result = ai_service.generate_translation(a_paper)
        assert result is not None
        assert result.title_translated == "Bir Makale"
        assert PaperTranslation.query.filter_by(paper_id=a_paper.id).count() == 1


def test_get_or_generate_translation_cache_hit(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda: True)
    with app.app_context():
        _db.session.add(
            PaperTranslation(paper_id=a_paper.id, target_lang="tr", title_translated="Önbellek")
        )
        _db.session.commit()

        def _boom(**kw):
            raise AssertionError("Claude should not be called on a cache hit")

        monkeypatch.setattr(ai_service, "_call_claude", _boom)
        result = ai_service.get_or_generate_translation(a_paper)
        assert result.title_translated == "Önbellek"
