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
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (_FAKE_ANALYSIS, "{}"))
    with app.app_context():
        result = ai_service.generate_analysis(a_paper)
        assert result is not None
        assert result.tldr == "kısa özet"
        assert result.method == ["yöntem 1", "yöntem 2"]
        # Cached row is now present.
        assert ai_service.get_analysis(a_paper) is not None
        assert PaperAnalysis.query.filter_by(paper_id=a_paper.id).count() == 1


def test_generate_analysis_disabled_returns_none(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)
    with app.app_context():
        assert ai_service.generate_analysis(a_paper) is None
        assert PaperAnalysis.query.filter_by(paper_id=a_paper.id).count() == 0


def test_generate_analysis_claude_failure_no_cache(app, a_paper, monkeypatch):
    """A failed LLM call returns None and must not persist a partial row."""
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (None, None))
    with app.app_context():
        assert ai_service.generate_analysis(a_paper) is None
        assert PaperAnalysis.query.filter_by(paper_id=a_paper.id).count() == 0


def test_get_or_generate_analysis_cache_hit_skips_claude(app, a_paper, monkeypatch):
    """When a row already exists, the LLM must not be called again."""
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    with app.app_context():
        _db.session.add(PaperAnalysis(paper_id=a_paper.id, target_lang="tr", tldr="mevcut"))
        _db.session.commit()

        def _boom(**kw):
            raise AssertionError("LLM should not be called on a cache hit")

        monkeypatch.setattr(ai_service, "_call_llm", _boom)
        result = ai_service.get_or_generate_analysis(a_paper)
        assert result.tldr == "mevcut"


def test_generate_translation_persists(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(
        ai_service,
        "_call_llm",
        lambda **kw: ({"title_translated": "Bir Makale", "abstract_translated": "Bir özet."}, "{}"),
    )
    with app.app_context():
        result = ai_service.generate_translation(a_paper)
        assert result is not None
        assert result.title_translated == "Bir Makale"
        assert PaperTranslation.query.filter_by(paper_id=a_paper.id).count() == 1


def test_get_or_generate_translation_cache_hit(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    with app.app_context():
        _db.session.add(
            PaperTranslation(paper_id=a_paper.id, target_lang="tr", title_translated="Önbellek")
        )
        _db.session.commit()

        def _boom(**kw):
            raise AssertionError("LLM should not be called on a cache hit")

        monkeypatch.setattr(ai_service, "_call_llm", _boom)
        result = ai_service.get_or_generate_translation(a_paper)
        assert result.title_translated == "Önbellek"


# --------------------------------------------------------------------------
# Video summary cache behaviour (Faz 3 — channel ingestion + transcript
# summarization). Same cache-first / never-poison-on-failure contract as
# analysis/translation above, plus a blank-transcript short-circuit that has
# no equivalent there (a paper always has *some* title/abstract; a video may
# genuinely have no transcript).
# --------------------------------------------------------------------------

_FAKE_VIDEO_SUMMARY = {
    "tldr": "video kısa özeti",
    "highlights": ["nokta 1", "nokta 2", "nokta 3"],
    "topics": ["ai", "ml"],
}


def test_generate_video_summary_ai_disabled_no_call(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)

    def _boom(**kw):
        raise AssertionError("LLM must not be called when AI is disabled")

    monkeypatch.setattr(ai_service, "_call_llm", _boom)
    with app.app_context():
        assert ai_service.generate_video_summary(a_paper, "some transcript text") is None
        assert ai_service.get_video_summary(a_paper) is None


def test_generate_video_summary_persists_with_transcript_chars(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (_FAKE_VIDEO_SUMMARY, "{}"))
    with app.app_context():
        transcript = "x" * 50_000  # longer than VIDEO_TRANSCRIPT_PROMPT_CHARS
        result = ai_service.generate_video_summary(a_paper, transcript, source_lang="en")
        assert result is not None
        assert result.tldr == "video kısa özeti"
        assert result.highlights == ["nokta 1", "nokta 2", "nokta 3"]
        assert result.topics == ["ai", "ml"]
        # Full pre-truncation length, not the prompt-capped length.
        assert result.transcript_chars == 50_000
        assert result.source_lang == "en"
        assert result.target_lang == "tr"
        assert ai_service.get_video_summary(a_paper).id == result.id


def test_generate_video_summary_second_call_updates_in_place(app, a_paper, monkeypatch):
    """The unique constraint on paper_id would raise on a second insert —
    this proves the upsert updates the existing row instead."""
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (_FAKE_VIDEO_SUMMARY, "{}"))
    with app.app_context():
        first = ai_service.generate_video_summary(a_paper, "transcript one")
        updated_fake = {**_FAKE_VIDEO_SUMMARY, "tldr": "güncellenmiş özet"}
        monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (updated_fake, "{}"))
        second = ai_service.generate_video_summary(a_paper, "transcript two")

        assert second.id == first.id
        assert second.tldr == "güncellenmiş özet"
        from app.modules.scrape.models import VideoSummary

        assert VideoSummary.query.filter_by(paper_id=a_paper.id).count() == 1


def test_generate_video_summary_llm_failure_no_cache(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (None, None))
    with app.app_context():
        assert ai_service.generate_video_summary(a_paper, "some transcript") is None
        assert ai_service.get_video_summary(a_paper) is None


def test_generate_video_summary_blank_transcript_no_call(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)

    def _boom(**kw):
        raise AssertionError("LLM must not be called for a blank transcript")

    monkeypatch.setattr(ai_service, "_call_llm", _boom)
    with app.app_context():
        assert ai_service.generate_video_summary(a_paper, "") is None
        assert ai_service.generate_video_summary(a_paper, None) is None
        assert ai_service.generate_video_summary(a_paper, "   ") is None


def test_get_or_generate_video_summary_cache_hit_skips_llm(app, a_paper, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    with app.app_context():
        from app.modules.scrape.models import VideoSummary

        _db.session.add(VideoSummary(paper_id=a_paper.id, tldr="mevcut özet"))
        _db.session.commit()

        def _boom(**kw):
            raise AssertionError("LLM should not be called on a cache hit")

        monkeypatch.setattr(ai_service, "_call_llm", _boom)
        result = ai_service.get_or_generate_video_summary(a_paper)
        assert result.tldr == "mevcut özet"
