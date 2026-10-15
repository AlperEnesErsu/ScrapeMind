"""LLM integration — provider abstraction + paper analysis/translation/chat
cache + digest generation.

Provider abstraction (Faz 1): the app talks to Anthropic Claude *or* any
OpenAI-compatible backend (OpenRouter, Ollama) through one dispatcher,
`_call_llm`. `LLM_PROVIDER` picks the backend; for OpenRouter, a per-user key
(encrypted in `UserSettings.settings["llm"]`) wins over the deployment-wide
`OPENROUTER_API_KEY` fallback. This is what makes AI features cost ~0 for a
self-hosted deployment — point at a `:free` OpenRouter model or a local
Ollama Qwen and nobody needs an Anthropic key.

Three public flows built on top of the dispatcher:

  * get_or_generate_analysis(paper, target_lang="tr", *, force=False, user=None)
      → returns a PaperAnalysis (or None if AI is disabled / call failed).
  * get_or_generate_translation(paper, target_lang="tr", *, force=False, user=None)
      → same shape, for the Title+Abstract translation.
  * generate_digest(user, links, *, period, period_start, period_end, target_lang="tr")
      → returns a UserDigest (or None) summarising a window of newly
        surfaced papers — see Bölüm B of the digest plan. period_start/end
        are the caller's window bounds (see app/tasks/digest_tasks.py),
        passed in explicitly so the upsert key is stable across retries.

All three are *idempotent under cache/upsert* — repeat calls do not
needlessly re-hit the LLM. The route layer owns the user-facing "trigger"
UI; this module just owns the model + cache + LLM wire-up.

`is_ai_enabled(user=None)` reports whether a usable provider+key can be
resolved for this user (or globally, if user is None) — templates/routes
branch on it to show a "configure to enable" hint instead of a crash.

Failure handling: on any LLM error (no key, timeout, parse failure) we log
+ return None and let the route render a "couldn't generate" panel. The
cache is never poisoned with a partial / error response. Transient errors
(429, 5xx, timeout, connection error) get up to `LLM_MAX_RETRIES` retries
with backoff before giving up — see `_call_claude`/`_call_openai_compatible`
and `_classify_llm_error`; permanent errors (400/401/403/404/parse failure)
never retry, they just cost money for the same result.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from flask import current_app

from app.extensions import db

logger = structlog.get_logger()

# Use a recent Claude model. Override via ANTHROPIC_MODEL env var in
# production if you want to test 4.8 / Opus / Haiku tiers.
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS_ANALYSIS = 1200
MAX_TOKENS_TRANSLATION = 1500
MAX_TOKENS_DIGEST = 1500
MAX_TOKENS_CHAT = 800
MAX_TOKENS_FEED_SCORE = 1200

# Digest window is capped to keep prompts small (and free-tier models fast) —
# a burst week of scraping should never balloon into a 200-item prompt.
DIGEST_MAX_ITEMS = 30

# Same cost guard for feed relevance scoring — only the newest N unscored
# announcements go into the one batch LLM call per user.
FEED_SCORE_MAX_ITEMS = 20

MAX_TOKENS_TOPIC_CLASSIFY = 300

# Channel-video transcript summarization (Faz 3 — agent reach).
MAX_TOKENS_VIDEO_SUMMARY = 1200
# Auto-generated transcripts run long; this caps how much of one goes into the
# prompt, same cost-guard reasoning as DIGEST_MAX_ITEMS/FEED_SCORE_MAX_ITEMS.
VIDEO_TRANSCRIPT_PROMPT_CHARS = 12_000

# Prior-art novelty assessment (Faz 5.2). Fewer items than the digest: the
# user is reading every one of these carefully, and a long list dilutes the
# comparison the assessment is actually for.
MAX_TOKENS_NOVELTY = 1200
NOVELTY_MAX_ITEMS = 12
NOVELTY_ABSTRACT_CHARS = 700

# Retrospective report map/reduce (Faz 6). The map step runs once per chunk
# (e.g. one per year, or one per "top N cited" slice) — report_service.py
# owns chunking + the numeric `stats` backbone, this module only ever sees
# one chunk's items or the full set of chunk summaries at a time.
MAX_TOKENS_REPORT_MAP = 900
MAX_TOKENS_REPORT_REDUCE = 2000
REPORT_ABSTRACT_CHARS = 600  # abstract slice that goes into each map item


# ----------------------------------------------------------------------------
# Provider abstraction (Bölüm B0) — Anthropic / OpenRouter / Ollama
# ----------------------------------------------------------------------------


def _llm_provider() -> str:
    return (current_app.config.get("LLM_PROVIDER") or "openrouter").strip().lower()


def _user_settings_row(user):
    """Fetch this user's UserSettings row by a direct query rather than the
    `user.settings` relationship attribute — the relationship can be stale
    (cached as None) on a `user` object that was loaded before a settings
    row was created earlier in the same session/request."""
    from app.core.models.settings import UserSettings

    return UserSettings.query.filter_by(user_id=user.id).first()


def get_user_llm_key(user) -> tuple[str | None, str | None]:
    """Decrypted (api_key, model_override) for this user's OpenRouter
    settings, read from `UserSettings.settings["llm"]`. Returns (None, None)
    if the user has no row / no key stored. Never logs the plaintext key."""
    if user is None:
        return None, None
    settings_row = _user_settings_row(user)
    if settings_row is None or not settings_row.settings:
        return None, None
    llm = (settings_row.settings or {}).get("llm") or {}
    model = (llm.get("model") or "").strip() or None
    enc = llm.get("api_key_enc")
    if not enc:
        return None, model
    return decrypt_llm_key(enc), model


def _resolve_llm(user=None) -> tuple[str, str | None, str, str] | None:
    """Resolve (provider, base_url, api_key, model) for this call, or None
    if nothing usable is configured (→ AI disabled for this user/context).

    Resolution order for openrouter (the default): user's own key first,
    then the deployment-wide OPENROUTER_API_KEY fallback. Ollama needs no
    key (local inference) — a dummy value is returned so callers have a
    uniform contract. Anthropic uses the existing global ANTHROPIC_API_KEY.
    """
    provider = _llm_provider()

    if provider == "anthropic":
        api_key = (current_app.config.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            return None
        return "anthropic", None, api_key, _model()

    if provider == "ollama":
        base_url = current_app.config.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
        model = current_app.config.get("OLLAMA_MODEL") or "qwen2.5"
        return "ollama", base_url, "ollama", model

    # Default: openrouter
    user_key, user_model = get_user_llm_key(user)
    api_key = user_key or (current_app.config.get("OPENROUTER_API_KEY") or "").strip() or None
    if not api_key:
        return None
    base_url = current_app.config.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    model = (
        user_model
        or current_app.config.get("OPENROUTER_MODEL")
        or "qwen/qwen-2.5-72b-instruct:free"
    )
    return "openrouter", base_url, api_key, model


def is_ai_enabled(user=None) -> bool:
    """True iff a provider + key can be resolved for this user (or globally
    if user is None). Cheap, no network call — used by templates/routes on
    every AI-tab render to pick the right empty state."""
    return _resolve_llm(user) is not None


def _model_label(user=None) -> str:
    """Model identifier to persist as `model_version` on generated rows."""
    resolved = _resolve_llm(user)
    return resolved[3] if resolved else _model()


def _client():
    """Lazy import + construct the Anthropic client. Importing at module
    level would force every test/dev workstation to install the SDK; this
    way the package is only required when AI is actually exercised.

    Explicit timeout (`LLM_CONNECT_TIMEOUT_SECONDS`/`LLM_TIMEOUT_SECONDS`) —
    without one, a hung provider had nothing to stop it short of Celery's
    600s soft limit, pinning an `llm` worker for 10 minutes over one call.

    `max_retries=0`: the SDK has its own built-in retry (transient errors,
    exponential backoff + jitter, honours Retry-After — see
    `anthropic._base_client.SyncAPIClient._should_retry`), but `_call_claude`
    now runs its own retry loop around this client. Leaving both on would
    multiply worst-case attempts (this client's own up-to-3 tries *times*
    `_call_claude`'s up-to-`LLM_MAX_RETRIES + 1`) instead of just adding to
    it — one retrier here, one place that owns the budget.
    """
    from anthropic import Anthropic, Timeout

    return Anthropic(
        api_key=current_app.config["ANTHROPIC_API_KEY"],
        timeout=Timeout(
            current_app.config.get("LLM_TIMEOUT_SECONDS", 120),
            connect=current_app.config.get("LLM_CONNECT_TIMEOUT_SECONDS", 10),
        ),
        max_retries=0,
    )


def _model() -> str:
    return current_app.config.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


# ----------------------------------------------------------------------------
# LLM call helpers
# ----------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """Some models occasionally wrap JSON in ``` fences. Be tolerant."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


# Retry policy for transient LLM failures (429/5xx/timeout/connection error).
# Both `_call_claude` and `_call_openai_compatible` run their own copy of this
# loop (not a shared one in `_call_llm`) so that
# tests/modules/test_digest.py's dispatcher tests — which monkeypatch
# `_call_claude`/`_call_openai_compatible` wholesale and assert `_call_llm`
# calls them directly — keep working unmodified; `_call_llm` itself is
# untouched by this task.
_RETRY_BASE_DELAY_SECONDS = 1.0
_RETRY_MAX_DELAY_SECONDS = 20.0
# A misbehaving/hostile backend's Retry-After must not be able to park an
# `llm` worker for an hour — same "respect it, but cap it" spirit as
# patentsview_source._get's single-hop Retry-After handling, generalised
# here to multiple attempts.
_RETRY_AFTER_CAP_SECONDS = 30.0


def _classify_llm_error(exc: Exception) -> tuple[bool, float | None]:
    """(is_transient, retry_after_seconds) for an exception raised by the
    Anthropic or OpenAI SDK's client call.

    Duck-typed against the two SDKs' exception hierarchies rather than
    importing either's exception classes at module level (same reasoning as
    the lazy `from anthropic import ...` in `_client()`). Both are
    Stainless-generated and structurally identical: `APIStatusError` carries
    `.status_code` + `.response`; `APIConnectionError`/`APITimeoutError`
    carry neither. Restricted to `type(exc).__module__` in
    `{"anthropic", "openai"}` so a bug in *our* code — some unrelated
    exception that also happens to lack `status_code` — is never mistaken
    for "no HTTP response yet, try again".

    Transient: 429, any 5xx, and connection/timeout errors. Permanent:
    everything else (400/401/403/404/... never retried — they cost money and
    won't fix themselves on the next attempt).
    """
    if type(exc).__module__ not in ("anthropic", "openai"):
        return False, None
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        # APIConnectionError / APITimeoutError: never got an HTTP response.
        return True, None
    if status_code != 429 and status_code < 500:
        return False, None
    retry_after = None
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", None)
    value = header.get("retry-after") if header is not None else None
    if value:
        try:
            retry_after = float(value)
        except (TypeError, ValueError):
            retry_after = None
    return True, retry_after


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    """Delay before retry attempt `attempt` (0-based: attempt 0 is the wait
    after the 1st failure). Honours the provider's Retry-After header when
    present, capped at `_RETRY_AFTER_CAP_SECONDS`; otherwise exponential
    backoff with jitter (50%-100% of the exponential value) so a batch of
    workers retrying the same outage don't all wake up in lockstep."""
    import random

    if retry_after is not None and retry_after > 0:
        return min(retry_after, _RETRY_AFTER_CAP_SECONDS)
    base = min(_RETRY_BASE_DELAY_SECONDS * (2**attempt), _RETRY_MAX_DELAY_SECONDS)
    return base * (0.5 + random.random() * 0.5)


def _call_claude_once(
    *, system: str, user_msg: str, max_tokens: int, expect_json: bool = True
) -> tuple[Any | None, str | None]:
    """One Claude call, no retry — SDK errors (network, 4xx/5xx, timeout)
    propagate to the caller (`_call_claude`) so it can classify transient vs.
    permanent and retry accordingly. A parse failure is not an exception
    (nothing to retry — the model answered, just not with JSON), so it is
    still handled here and returned as `(None, raw)`."""
    client = _client()
    resp = client.messages.create(
        model=_model(),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    # The SDK returns a list of content blocks; we expect a single text block.
    raw = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            raw += getattr(block, "text", "")
    if not raw:
        logger.warning("claude_empty_response", model=_model())
        return None, None
    if not expect_json:
        return raw.strip(), raw
    try:
        parsed = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        logger.warning("claude_json_parse_failed", preview=raw[:200])
        return None, raw
    return parsed, raw


def _call_claude(
    *, system: str, user_msg: str, max_tokens: int, expect_json: bool = True
) -> tuple[Any | None, str | None]:
    """Single Claude call returning (parsed_json_or_text, raw_text). Either
    may be None on failure — caller decides what to do. `expect_json=False`
    skips JSON parsing and returns the stripped raw text as the first
    element (used by the RAG chat, which wants prose, not JSON).

    Retries up to `LLM_MAX_RETRIES` times on a transient failure (429, 5xx,
    timeout, connection error — see `_classify_llm_error`), with exponential
    backoff + jitter (or the provider's own Retry-After, capped). A
    permanent failure (400/401/403/404/no key/parse failure) returns on the
    first attempt — retrying it would just spend money for the same result.
    Still never raises: total exhaustion returns `(None, None)`, same as
    before this call ever had a retry loop.
    """
    max_retries = max(0, int(current_app.config.get("LLM_MAX_RETRIES", 2)))
    for attempt in range(max_retries + 1):
        try:
            return _call_claude_once(
                system=system, user_msg=user_msg, max_tokens=max_tokens, expect_json=expect_json
            )
        except Exception as exc:
            is_transient, retry_after = _classify_llm_error(exc)
            if not is_transient or attempt >= max_retries:
                # Network error, missing key, model not available, or
                # retries exhausted — log + give up.
                logger.exception("claude_call_failed", attempt=attempt + 1, transient=is_transient)
                return None, None
            delay = _retry_delay(attempt, retry_after)
            logger.warning(
                "claude_call_retrying",
                attempt=attempt + 1,
                max_retries=max_retries,
                delay_seconds=round(delay, 2),
            )
            time.sleep(delay)
    return None, None  # pragma: no cover - loop above always returns


def _call_openai_compatible_once(
    *,
    system: str,
    user_msg: str,
    max_tokens: int,
    base_url: str,
    api_key: str,
    model: str,
    expect_json: bool = True,
) -> tuple[Any | None, str | None]:
    """One call against an OpenAI-compatible chat completions endpoint
    (OpenRouter or Ollama), no retry — SDK errors propagate to the caller
    (`_call_openai_compatible`) so it can classify transient vs. permanent
    and retry accordingly. Same (parsed, raw) contract as `_call_claude_once`.

    `max_retries=0` and the explicit timeout mirror `_client()`'s reasoning
    exactly: the SDK's own built-in retry must not stack with
    `_call_openai_compatible`'s.

    Note: the "retry once without response_format" fallback below is a
    same-attempt backend-compatibility shim, not a transient-failure retry.
    It fires only on a *permanent* error, so a 401/400 against a backend that
    also dislikes `response_format` can cost 2 HTTP requests before the real
    error surfaces — but a 429/5xx propagates on the first request, leaving
    the pacing to `_call_openai_compatible`'s backoff loop instead of
    doubling the rate against a backend that asked us to slow down.
    """
    from openai import OpenAI, Timeout

    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=Timeout(
            current_app.config.get("LLM_TIMEOUT_SECONDS", 120),
            connect=current_app.config.get("LLM_CONNECT_TIMEOUT_SECONDS", 10),
        ),
        max_retries=0,
        default_headers={
            "HTTP-Referer": "https://github.com/scrapemind",
            "X-Title": "ScrapeMind",
        },
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    }
    if expect_json:
        # Not every model/backend honours this (small/local models in
        # particular) — best-effort only, tolerant parsing below covers
        # the rest.
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        # Some backends (older Ollama builds) reject `response_format` — drop
        # it and try once more.
        #
        # Only for a *permanent* error, though. A 429 or 5xx says nothing
        # about whether the backend understands `response_format`, and
        # retrying it here would double our request rate against an endpoint
        # that just asked us to slow down — cancelling out the backoff in
        # `_call_openai_compatible`, which is the layer that owns transient
        # failures. Let those propagate untouched.
        is_transient, _ = _classify_llm_error(exc)
        if is_transient or not expect_json or "response_format" not in kwargs:
            raise
        kwargs.pop("response_format")
        resp = client.chat.completions.create(**kwargs)

    raw = (resp.choices[0].message.content or "") if resp.choices else ""
    if not raw:
        logger.warning("openai_compatible_empty_response", model=model)
        return None, None
    if not expect_json:
        return raw.strip(), raw
    try:
        parsed = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        logger.warning("openai_compatible_json_parse_failed", model=model, preview=raw[:200])
        return None, raw
    return parsed, raw


def _call_openai_compatible(
    *,
    system: str,
    user_msg: str,
    max_tokens: int,
    base_url: str,
    api_key: str,
    model: str,
    expect_json: bool = True,
) -> tuple[Any | None, str | None]:
    """Single call against an OpenAI-compatible chat completions endpoint
    (OpenRouter or Ollama). Same (parsed, raw) contract as `_call_claude`.

    Retries up to `LLM_MAX_RETRIES` times on a transient failure (429, 5xx,
    timeout, connection error — see `_classify_llm_error`), with exponential
    backoff + jitter (or the provider's own Retry-After, capped). A
    permanent failure returns on the first attempt. Still never raises.

    Worst case, with the default `LLM_MAX_RETRIES=2` (3 attempts): 4 HTTP
    requests. A transient failure costs exactly one request per attempt (the
    `response_format` fallback in `_call_openai_compatible_once` deliberately
    does not fire for those), so the ceiling is two transient attempts at one
    request each plus a final permanent one that also trips the fallback.
    `generate_digest`'s separate JSON-repair retry can call this function
    twice, so one digest tops out at 8 requests.
    """
    max_retries = max(0, int(current_app.config.get("LLM_MAX_RETRIES", 2)))
    for attempt in range(max_retries + 1):
        try:
            return _call_openai_compatible_once(
                system=system,
                user_msg=user_msg,
                max_tokens=max_tokens,
                base_url=base_url,
                api_key=api_key,
                model=model,
                expect_json=expect_json,
            )
        except Exception as exc:
            is_transient, retry_after = _classify_llm_error(exc)
            if not is_transient or attempt >= max_retries:
                logger.exception(
                    "openai_compatible_call_failed",
                    model=model,
                    attempt=attempt + 1,
                    transient=is_transient,
                )
                return None, None
            delay = _retry_delay(attempt, retry_after)
            logger.warning(
                "openai_compatible_call_retrying",
                model=model,
                attempt=attempt + 1,
                max_retries=max_retries,
                delay_seconds=round(delay, 2),
            )
            time.sleep(delay)
    return None, None  # pragma: no cover - loop above always returns


def _call_llm(
    *, system: str, user_msg: str, max_tokens: int, user=None, expect_json: bool = True
) -> tuple[Any | None, str | None]:
    """Provider-agnostic dispatcher — resolves the backend for `user` (see
    `_resolve_llm`) and routes to Claude or an OpenAI-compatible backend.
    Same (parsed, raw) return contract as `_call_claude`. Returns (None,
    None) if no provider/key is resolvable (AI disabled)."""
    resolved = _resolve_llm(user)
    if resolved is None:
        return None, None
    provider, base_url, api_key, model = resolved
    if provider == "anthropic":
        return _call_claude(
            system=system, user_msg=user_msg, max_tokens=max_tokens, expect_json=expect_json
        )
    return _call_openai_compatible(
        system=system,
        user_msg=user_msg,
        max_tokens=max_tokens,
        base_url=base_url,
        api_key=api_key,
        model=model,
        expect_json=expect_json,
    )


# ----------------------------------------------------------------------------
# Per-user API key storage (UserSettings.settings["llm"]), Fernet-encrypted
# ----------------------------------------------------------------------------


# Moved to `credentials.py` when Zotero became the second consumer. Kept as
# names here because callers and tests import them from this module, and the
# derivation is byte-identical -- keys encrypted before the move still decrypt.
def encrypt_llm_key(plain: str) -> str:
    """Encrypt a plaintext API key for storage. Never call this on anything
    you intend to log."""
    from app.modules.scrape.credentials import encrypt_secret

    return encrypt_secret(plain)


def decrypt_llm_key(token: str) -> str | None:
    """Decrypt a stored key. Returns None (and logs, without the plaintext)
    on any failure — a corrupt/rotated-key row degrades to "no key" instead
    of crashing the request."""
    from app.modules.scrape.credentials import decrypt_secret

    return decrypt_secret(token)


def set_user_llm_key(user, api_key: str | None, model: str | None = None) -> None:
    """Upsert this user's OpenRouter key + optional model override. An empty/
    None api_key leaves the existing stored key untouched (use
    `clear_user_llm_key` to remove it) — this lets the settings form update
    just the model override without re-entering the key."""
    from app.core.models.settings import UserSettings

    settings_row = _user_settings_row(user)
    if settings_row is None:
        settings_row = UserSettings(user_id=user.id, settings={})
        db.session.add(settings_row)

    data = dict(settings_row.settings or {})
    llm = dict(data.get("llm") or {})

    key = (api_key or "").strip()
    if key:
        llm["api_key_enc"] = encrypt_llm_key(key)

    model_clean = (model or "").strip()
    if model_clean:
        llm["model"] = model_clean
    elif model is not None:
        # Explicit blank submitted — clear the override.
        llm.pop("model", None)

    data["llm"] = llm
    settings_row.settings = data
    db.session.commit()


def clear_user_llm_key(user) -> None:
    """Remove the user's stored key + model override entirely."""
    settings_row = _user_settings_row(user)
    if settings_row is None:
        return
    data = dict(settings_row.settings or {})
    if "llm" in data:
        data.pop("llm")
        settings_row.settings = data
        db.session.commit()


def masked_key_preview(key: str | None) -> str | None:
    """`sk-or-...••••1234` style preview — never render the full key."""
    if not key:
        return None
    tail = key[-4:] if len(key) >= 4 else key
    head = key[:6] if len(key) > 10 else ""
    return f"{head}••••{tail}" if head else f"••••{tail}"


def user_llm_status(user) -> dict:
    """Non-secret summary for the AI settings tab template: has_key, masked
    preview, and the model override (if any). Never exposes the plaintext
    key."""
    key, model = get_user_llm_key(user)
    return {
        "has_key": bool(key),
        "masked": masked_key_preview(key),
        "model": model,
    }


# ----------------------------------------------------------------------------
# Re-exports: the feature code lives in app/modules/scrape/ai/. It reads the
# plumbing above through `ai_service.<name>` at call time, so patching a name on
# this module still works. Keep these imports at the bottom (circular import).
# ----------------------------------------------------------------------------
from app.modules.scrape.ai.analysis import (  # noqa: E402, F401
    _ANALYSIS_SYSTEM_TR,
    _TRANSLATION_SYSTEM_TR,
    _VIDEO_SUMMARY_SYSTEM_TR,
    _WEB_SUMMARY_INPUT_CHARS,
    FULLTEXT_ANALYSIS_CHARS,
    MAX_TOKENS_WEB_SUMMARY,
    _analysis_user_msg,
    ask_paper,
    generate_analysis,
    generate_translation,
    generate_video_summary,
    get_analysis,
    get_or_generate_analysis,
    get_or_generate_translation,
    get_or_generate_video_summary,
    get_translation,
    get_video_summary,
    summarize_web_content,
)
from app.modules.scrape.ai.learning_narration import (  # noqa: E402, F401
    _LEARNING_MAP_LANGUAGES,
    _LEARNING_MAP_SYSTEM_TR,
    LEARNING_MAP_EXPLAIN_CHARS,
    LEARNING_MAP_LABEL_CHARS,
    LEARNING_MAP_QUESTION_CHARS,
    MAX_TOKENS_LEARNING_MAP,
    _clip,
    _learning_map_language,
    narrate_learning_map,
)
from app.modules.scrape.ai.patents import (  # noqa: E402, F401
    _CLAIM_EXPLAIN_SYSTEM_TR,
    _NOVELTY_SYSTEM_TR,
    CLAIM_EXPLAIN_CHARS,
    CLAIM_EXPLAIN_MAX_TERMS,
    MAX_TOKENS_CLAIM_EXPLAIN,
    analyze_novelty,
    explain_patent_claim,
)
from app.modules.scrape.ai.reports import (  # noqa: E402, F401
    _AUTHOR_GROUP_REDUCE_SYSTEM_TR,
    _DIGEST_SYSTEM_TR,
    _REPORT_MAP_SYSTEM_TR,
    _REPORT_REDUCE_SYSTEM_TR,
    _truncate,
    generate_digest,
    summarize_report_chunk,
    synthesize_author_group_report,
    synthesize_report,
)
from app.modules.scrape.ai.topics import (  # noqa: E402, F401
    _FEED_SCORE_SYSTEM_TR,
    _KEYWORD_EN_LEXICON,
    _KEYWORD_TRANSLATE_SYSTEM_TR,
    _TOPIC_CLASSIFY_SYSTEM_TR,
    _TOPIC_LEXICON,
    KEYWORD_TRANSLATE_MAX_ITEMS,
    MAX_TOKENS_KEYWORD_TRANSLATE,
    _cached_user_topics,
    _lexicon_topics,
    _lexicon_translation,
    _safe_list,
    _safe_str,
    _store_user_topics,
    _topics_hash,
    classify_user_topics,
    score_feed_relevance,
    translate_keywords,
)
