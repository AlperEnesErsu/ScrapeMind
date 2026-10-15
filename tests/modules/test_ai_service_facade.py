"""`ai_service` is a facade over `scrape/ai/*`: names stay importable from it and
patching a name on it still reaches the feature code."""

from __future__ import annotations

import pytest

from app.modules.scrape import ai_service
from app.modules.scrape.ai import analysis, learning_narration, patents, reports, topics


@pytest.mark.parametrize(
    ("module", "names"),
    [
        (analysis, ["generate_analysis", "ask_paper", "summarize_web_content", "get_translation"]),
        (reports, ["generate_digest", "synthesize_report", "summarize_report_chunk"]),
        (topics, ["classify_user_topics", "translate_keywords", "score_feed_relevance"]),
        (patents, ["analyze_novelty", "explain_patent_claim"]),
        (learning_narration, ["narrate_learning_map"]),
    ],
)
def test_facade_reexports_the_feature_functions(module, names):
    for name in names:
        assert getattr(ai_service, name) is getattr(module, name)


def test_feature_code_reads_plumbing_through_the_facade(monkeypatch):
    seen = {}

    def fake_call_llm(**kwargs):
        seen.update(kwargs)
        return "  Kısa özet.  ", "raw"

    # Patched on `ai_service`, used by a function that now lives in `ai.analysis`.
    monkeypatch.setattr(ai_service, "_call_llm", fake_call_llm)
    assert ai_service.summarize_web_content("Başlık", "Metin") == "Kısa özet."
    assert seen["max_tokens"] == ai_service.MAX_TOKENS_WEB_SUMMARY


def test_patched_constant_is_honoured_by_moved_code(monkeypatch):
    seen = {}

    def fake_call_llm(**kwargs):
        seen.update(kwargs)
        return "ok", "raw"

    monkeypatch.setattr(ai_service, "_call_llm", fake_call_llm)
    monkeypatch.setattr(ai_service, "_WEB_SUMMARY_INPUT_CHARS", 5)
    ai_service.summarize_web_content("T", "abcdefghij")
    assert seen["user_msg"].endswith("abcde")
