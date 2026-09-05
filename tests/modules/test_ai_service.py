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


# ----------------------------------------------------------------------------
# Timeouts + retry (fix/llm-resilience) — both SDK clients get an explicit
# timeout, and `_call_claude`/`_call_openai_compatible` retry transient
# failures (429/5xx/timeout/connection error) up to LLM_MAX_RETRIES times,
# never permanent ones (400/401/403/404). No real network: the SDK client
# classes themselves are monkeypatched, and `time.sleep` is stubbed so the
# backoff delays don't actually slow the test run down.
#
# `_call_llm` is intentionally NOT touched by this feature (still just
# dispatches to `_call_claude`/`_call_openai_compatible`, unchanged) because
# tests/modules/test_digest.py's dispatcher tests monkeypatch those two
# functions wholesale and assert `_call_llm` calls them directly — putting
# the retry loop there instead would make those tests observe the raw,
# un-retried functions.
# ----------------------------------------------------------------------------

import httpx  # noqa: E402 — grouped near the tests that need it, not the module's normal imports


def _rate_limit_error(retry_after=None):
    """A real `anthropic.RateLimitError` (429), optionally carrying a
    Retry-After header — used to exercise `_classify_llm_error` /
    `_retry_delay` against the actual SDK exception shape rather than a
    hand-rolled stand-in."""
    import anthropic

    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, headers=headers, request=request)
    return anthropic.RateLimitError("rate limited", response=response, body=None)


def _server_error():
    import anthropic

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(500, request=request)
    return anthropic.InternalServerError("server error", response=response, body=None)


def _bad_request_error():
    import anthropic

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request)
    return anthropic.BadRequestError("bad request", response=response, body=None)


def _openai_rate_limit_error(retry_after=None):
    import openai

    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(429, headers=headers, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def _openai_bad_request_error():
    import openai

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return openai.BadRequestError("bad request", response=response, body=None)


class _FakeAnthropicClient:
    """Stands in for `anthropic.Anthropic` — records constructor kwargs and
    lets the test script a canned sequence of `messages.create` results."""

    captured_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeAnthropicClient.captured_kwargs = kwargs
        self.messages = self

    def create(self, **kwargs):
        raise NotImplementedError  # overridden per-test via monkeypatch


class _FakeMessageBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeMessageResponse:
    def __init__(self, text):
        self.content = [_FakeMessageBlock(text)]


class _FakeOpenAIClient:
    """Stands in for `openai.OpenAI` — records constructor kwargs; chat
    completion behaviour is scripted per-test onto `.chat.completions.create`."""

    captured_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeOpenAIClient.captured_kwargs = kwargs
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        raise NotImplementedError  # overridden per-test via monkeypatch


def _fake_openai_response(text):
    from types import SimpleNamespace

    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_client_passes_configured_timeout_and_disables_sdk_retries(app, monkeypatch):
    """`_client()` must hand the Anthropic SDK an explicit connect/read
    timeout built from LLM_CONNECT_TIMEOUT_SECONDS/LLM_TIMEOUT_SECONDS, and
    max_retries=0 (our own retry loop owns retries now, not the SDK's)."""
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setitem(app.config, "LLM_CONNECT_TIMEOUT_SECONDS", 7)
    monkeypatch.setitem(app.config, "LLM_TIMEOUT_SECONDS", 55)
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropicClient)
    with app.app_context():
        client = ai_service._client()
        assert isinstance(client, _FakeAnthropicClient)
        kwargs = _FakeAnthropicClient.captured_kwargs
        assert kwargs["max_retries"] == 0
        timeout = kwargs["timeout"]
        assert timeout.connect == 7
        assert timeout.read == 55


def test_openai_compatible_passes_configured_timeout_and_disables_sdk_retries(app, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_CONNECT_TIMEOUT_SECONDS", 8)
    monkeypatch.setitem(app.config, "LLM_TIMEOUT_SECONDS", 42)
    monkeypatch.setattr("openai.OpenAI", _FakeOpenAIClient)

    def _create(**kwargs):
        return _fake_openai_response('{"ok": true}')

    _FakeOpenAIClient.create = staticmethod(_create)
    with app.app_context():
        parsed, _raw = ai_service._call_openai_compatible_once(
            system="s",
            user_msg="u",
            max_tokens=10,
            base_url="https://openrouter.ai/api/v1",
            api_key="k",
            model="m",
        )
        assert parsed == {"ok": True}
        kwargs = _FakeOpenAIClient.captured_kwargs
        assert kwargs["max_retries"] == 0
        timeout = kwargs["timeout"]
        assert timeout.connect == 8
        assert timeout.read == 42


def test_classify_llm_error_transient_cases():
    is_transient, retry_after = ai_service._classify_llm_error(_rate_limit_error("2.5"))
    assert is_transient is True
    assert retry_after == 2.5

    is_transient, retry_after = ai_service._classify_llm_error(_server_error())
    assert is_transient is True
    assert retry_after is None

    import anthropic

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    is_transient, retry_after = ai_service._classify_llm_error(
        anthropic.APITimeoutError(request=request)
    )
    assert is_transient is True
    assert retry_after is None


def test_classify_llm_error_permanent_cases():
    is_transient, retry_after = ai_service._classify_llm_error(_bad_request_error())
    assert is_transient is False
    assert retry_after is None

    # A non-SDK exception must never be treated as "safe to retry" just
    # because it lacks a status_code.
    is_transient, retry_after = ai_service._classify_llm_error(ValueError("boom"))
    assert is_transient is False
    assert retry_after is None


def test_retry_delay_caps_retry_after():
    assert ai_service._retry_delay(0, 999.0) == ai_service._RETRY_AFTER_CAP_SECONDS
    assert ai_service._retry_delay(0, 5.0) == 5.0


def test_retry_delay_backoff_without_retry_after():
    delay = ai_service._retry_delay(3, None)
    # Exponential base capped at _RETRY_MAX_DELAY_SECONDS, jittered to 50-100%.
    max_base = min(
        ai_service._RETRY_BASE_DELAY_SECONDS * (2**3), ai_service._RETRY_MAX_DELAY_SECONDS
    )
    assert 0 < delay <= max_base


def test_call_claude_retries_on_429_then_succeeds(app, monkeypatch):
    """A 429 gets retried; the call succeeds on the 2nd attempt and the
    dispatcher never sees a failure."""
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 2)
    monkeypatch.setattr("time.sleep", lambda s: None)

    calls = {"n": 0}

    def _fake_once(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit_error("0.01")
        return {"ok": True}, "{}"

    monkeypatch.setattr(ai_service, "_call_claude_once", _fake_once)
    with app.app_context():
        parsed, raw = ai_service._call_claude(system="s", user_msg="u", max_tokens=10)
        assert parsed == {"ok": True}
        assert calls["n"] == 2


def test_call_claude_does_not_retry_permanent_error(app, monkeypatch):
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 2)
    monkeypatch.setattr("time.sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))

    calls = {"n": 0}

    def _fake_once(**kw):
        calls["n"] += 1
        raise _bad_request_error()

    monkeypatch.setattr(ai_service, "_call_claude_once", _fake_once)
    with app.app_context():
        parsed, raw = ai_service._call_claude(system="s", user_msg="u", max_tokens=10)
        assert (parsed, raw) == (None, None)
        assert calls["n"] == 1  # no retry at all — a single attempt


def test_call_claude_retry_cap_is_respected(app, monkeypatch):
    """LLM_MAX_RETRIES=2 must mean at most 3 attempts total (1 + 2 retries),
    never more, even when every attempt is transient."""
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 2)
    monkeypatch.setattr("time.sleep", lambda s: None)

    calls = {"n": 0}

    def _fake_once(**kw):
        calls["n"] += 1
        raise _server_error()

    monkeypatch.setattr(ai_service, "_call_claude_once", _fake_once)
    with app.app_context():
        parsed, raw = ai_service._call_claude(system="s", user_msg="u", max_tokens=10)
        assert (parsed, raw) == (None, None)
        assert calls["n"] == 3


def test_call_claude_exhausted_retries_returns_none_none_never_raises(app, monkeypatch):
    """The module's long-standing contract: `_call_claude` never raises, even
    after every retry attempt failed."""
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 1)
    monkeypatch.setattr("time.sleep", lambda s: None)

    def _fake_once(**kw):
        raise _server_error()

    monkeypatch.setattr(ai_service, "_call_claude_once", _fake_once)
    with app.app_context():
        result = ai_service._call_claude(system="s", user_msg="u", max_tokens=10)
        assert result == (None, None)


def test_call_openai_compatible_retries_on_429_then_succeeds(app, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 2)
    monkeypatch.setattr("time.sleep", lambda s: None)

    calls = {"n": 0}

    def _fake_once(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _openai_rate_limit_error("0.01")
        return {"ok": True}, "{}"

    monkeypatch.setattr(ai_service, "_call_openai_compatible_once", _fake_once)
    with app.app_context():
        parsed, raw = ai_service._call_openai_compatible(
            system="s", user_msg="u", max_tokens=10, base_url="https://x", api_key="k", model="m"
        )
        assert parsed == {"ok": True}
        assert calls["n"] == 2


def test_call_openai_compatible_does_not_retry_permanent_error(app, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 2)
    monkeypatch.setattr("time.sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))

    calls = {"n": 0}

    def _fake_once(**kw):
        calls["n"] += 1
        raise _openai_bad_request_error()

    monkeypatch.setattr(ai_service, "_call_openai_compatible_once", _fake_once)
    with app.app_context():
        parsed, raw = ai_service._call_openai_compatible(
            system="s", user_msg="u", max_tokens=10, base_url="https://x", api_key="k", model="m"
        )
        assert (parsed, raw) == (None, None)
        assert calls["n"] == 1


def test_retry_after_header_is_capped(app, monkeypatch):
    """A Retry-After far above the cap must be clamped, not obeyed verbatim —
    otherwise a hostile/misconfigured backend could park a worker for an
    arbitrary amount of time."""
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 1)

    seen_delays = []
    monkeypatch.setattr("time.sleep", lambda s: seen_delays.append(s))

    calls = {"n": 0}

    def _fake_once(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit_error("999999")
        return {"ok": True}, "{}"

    monkeypatch.setattr(ai_service, "_call_claude_once", _fake_once)
    with app.app_context():
        parsed, _raw = ai_service._call_claude(system="s", user_msg="u", max_tokens=10)
        assert parsed == {"ok": True}
        assert len(seen_delays) == 1
        assert seen_delays[0] == ai_service._RETRY_AFTER_CAP_SECONDS
