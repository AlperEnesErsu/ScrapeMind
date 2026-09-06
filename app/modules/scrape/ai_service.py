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
from app.modules.scrape.models import (
    Paper,
    PaperAnalysis,
    PaperTranslation,
    UserDigest,
    VideoSummary,
)

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
# Prompts (kept inline so caching keys stay stable per release)
# ----------------------------------------------------------------------------

_ANALYSIS_SYSTEM_TR = """Sen, araştırmacıların akademik makaleleri hızlıca \
değerlendirmesine yardımcı olan bir asistan'sın. Cevaplarını DAİMA Türkçe \
ver ve şu JSON şemasına uygun döndür:

{
  "tldr": "2-3 cümlelik özet — neyi başardığı + neden önemli",
  "method": ["yöntem maddesi 1", "..."],
  "findings": ["bulgu maddesi 1", "..."],
  "limitations": ["kısıt maddesi 1", "..."],
  "personal_relevance": "Kullanıcının ilgi alanına neden uyuyor (1-2 cümle)."
}

method/findings/limitations her biri en fazla 4 madde, her madde tek \
satır. Sadece JSON döndür, başına/sonuna metin ekleme."""

_VIDEO_SUMMARY_SYSTEM_TR = """Sen, bir YouTube videosunun otomatik-oluşturulmuş \
transkriptinden kullanıcıya hızlı bir özet çıkaran bir asistan'sın. Sana \
verilen metin bir konuşma tanıma sisteminin çıktısıdır — noktalama işaretleri \
eksik olabilir, kelime hataları içerebilir ve zaman zaman anlamsız parçalar \
barındırabilir. Görevin transkripti tarif etmek DEĞİL, videonun içeriğini \
özetlemektir — sanki videoyu izlemiş gibi konuş.

Cevaplarını DAİMA Türkçe ver ve şu JSON şemasına uygun döndür:

{
  "tldr": "2-3 cümlelik özet — video ne anlatıyor, ana çıkarım ne",
  "highlights": ["öne çıkan nokta 1", "öne çıkan nokta 2", "..."],
  "topics": ["kısa konu etiketi 1", "kısa konu etiketi 2", "..."]
}

highlights en az 3, en fazla 6 madde içermeli, her biri tek satır. Sadece \
JSON döndür, başına/sonuna metin ekleme."""

_TRANSLATION_SYSTEM_TR = """Akademik metin çevirmenisin. Sana verilen \
İngilizce makale başlığı ve özetini akıcı, terimleri koruyan Türkçe'ye \
çevir. Çıktıyı şu JSON şemasında döndür:

{
  "title_translated": "...",
  "abstract_translated": "..."
}

Sadece JSON döndür."""

_DIGEST_SYSTEM_TR = """Sen bir araştırma-istihbarat asistanısın. Sana \
numaralandırılmış bir liste halinde, bir kullanıcının takip ettiği \
kaynaklardan son pencerede (gün/hafta) toplanan öğeler verilecek — bunlar hem \
akademik makaleler hem de sektör duyuruları (OpenAI/Google/DeepMind/Hugging \
Face gibi kaynaklardan blog/haber) olabilir. Görevin bunları kullanıcı için \
Türkçe bir brifing haline getirmek: neyi kaçırmamalı, hangi temalar öne çıkıyor.

Cevabını DAİMA Türkçe ver ve şu JSON şemasına uygun döndür:

{
  "summary": "2-4 cümlelik genel özet — bu pencerede öne çıkan gelişmeler",
  "highlights": [
    {"title": "makale başlığı (verilen listeden aynen kopyala)", "why": "neden önemli/dikkat çekici (1 cümle)", "ref": <listede verilen index numarası, integer>}
  ],
  "themes": ["kısa tema/başlık 1", "kısa tema/başlık 2", "..."]
}

highlights en fazla 8 madde, en önemli/ilgi çekici makaleleri seç. \
themes en fazla 5 madde. Sadece JSON döndür, başına/sonuna metin ekleme."""

_FEED_SCORE_SYSTEM_TR = """Sen bir araştırma-istihbarat asistanısın. Kullanıcının \
takip ettiği ilgi alanı anahtar kelimeleri ile numaralandırılmış bir sektör \
duyurusu (haber/blog) listesi verilecek. Görevin her duyurunun bu kullanıcı \
için ne kadar alakalı olduğunu değerlendirmek.

Cevabını DAİMA Türkçe ver ve şu JSON şemasına uygun döndür:

{
  "scores": [
    {"ref": <listede verilen index numarası, integer>, "score": <0-100 arası tam sayı, alaka düzeyi>, "why": "kısa Türkçe gerekçe (1 cümle)", "matched_keyword": "en alakalı anahtar kelime (yoksa null)"}
  ]
}

scores listesi verilen HER duyuru için tam olarak bir madde içermeli — hiçbirini \
atlama. Sadece JSON döndür, başına/sonuna metin ekleme."""

_REPORT_MAP_SYSTEM_TR = """Sen bir araştırma-istihbarat asistanısın. Sana bir \
retrospektif rapor için numaralandırılmış bir akademik çalışma listesi \
verilecek — bu liste raporun tamamı değil, yalnızca bir parçası (ör. tek bir \
yıl ya da en çok atıf alan bir grup). Görevin bu parçayı kısa bir ara-özete \
indirmek; nihai rapor başka bir adımda bütün parçalardan birleştirilecek.

Cevabını DAİMA Türkçe ver ve şu JSON şemasına uygun döndür:

{
  "themes": ["bu parçada öne çıkan tema/başlık 1", "..."],
  "notable": [
    {"ref": <listede verilen index numarası, integer>, "why": "neden dikkat çekici (1 cümle)"}
  ],
  "methods": ["öne çıkan yöntem/yaklaşım 1", "..."]
}

notable en fazla 5 madde, en dikkat çekici çalışmaları seç — "ref" alanı \
listede sana verilen numaralardan biri OLMALI, uydurma numara verme. themes \
ve methods her biri en fazla 5 madde. Sadece JSON döndür, başına/sonuna \
metin ekleme."""

_REPORT_REDUCE_SYSTEM_TR = """Sen bir araştırma-istihbarat asistanısın. Sana \
bir kullanıcının takip ettiği alanda son birkaç yılda ne olduğunu özetleyen \
bir retrospektif rapor hazırlaman için iki girdi verilecek: (1) LLM \
kullanılmadan hesaplanmış sayısal bir özet (yıl bazında sayılar, en üretken \
yazarlar/mekânlar, konu dağılımı, açık erişim oranı gibi) ve (2) bu sayısal \
özetin arkasındaki çalışmaların önceki bir adımda parça parça özetlenmiş \
hali. Görevin bu ikisini birleştirip kullanıcı için Türkçe, okunabilir bir \
rapor haline getirmek.

Cevabını DAİMA Türkçe ver ve şu JSON şemasına uygun döndür:

{
  "tldr": "2-4 cümlelik genel özet — bu dönemde alanda öne çıkan gelişmeler",
  "timeline": [
    {"year": <integer>, "themes": ["o yılın öne çıkan temaları"], "notable": ["o yıla ait dikkat çekici gelişme(ler)"]}
  ],
  "emerging": ["yükselişte olan tema/yöntem 1", "..."],
  "fading": ["ilgisi azalan tema/yöntem 1", "..."],
  "key_works": [{"title": "çalışma başlığı", "why": "neden önemli (1 cümle)"}],
  "key_authors": ["öne çıkan yazar 1", "..."],
  "key_venues": ["öne çıkan dergi/konferans 1", "..."],
  "for_your_keywords": "kullanıcının takip ettiği anahtar kelimelerle bu raporun bağlantısını özetleyen 1-2 cümle"
}

timeline'daki her yıl için en fazla 3 tema ve 3 dikkat çekici madde ver. \
emerging/fading/key_authors/key_venues her biri en fazla 6 madde, key_works \
en fazla 8 madde. Sadece verilen girdilere dayan, uydurma bilgi ekleme. \
Sadece JSON döndür, başına/sonuna metin ekleme."""

_TOPIC_CLASSIFY_SYSTEM_TR = """Sen bir araştırma ilgi alanı sınıflandırma \
asistanısın. Sana bir kullanıcının ilgi alanı anahtar kelimeleri verilecek. \
Görevin her kelimeyi şu sabit konu listesinden (taksonomiden) en uygun olan(lar)a \
eşlemek — yalnızca bu listedeki anahtarları kullan:

ai, ml, cs, physics, math, biomed, social, humanities, general

Cevabını şu JSON şemasına uygun döndür:

{
  "topics": ["konu_anahtarı_1", "konu_anahtarı_2", "..."]
}

topics listesi yukarıdaki taksonomiden seçilmiş, tekrarsız anahtar kelimelerden \
oluşmalı. Hiçbir kelime net bir konuya oturmuyorsa "general" kullan. Sadece JSON \
döndür, başına/sonuna metin ekleme."""


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


def _fernet_key() -> bytes:
    """32-byte urlsafe-base64 Fernet key. Uses LLM_ENC_KEY if configured,
    otherwise derives a stable one from SECRET_KEY so dev/test never need a
    separate secret."""
    raw = (current_app.config.get("LLM_ENC_KEY") or "").strip()
    if raw:
        return raw.encode("utf-8")
    import base64
    import hashlib

    secret = (current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    digest = hashlib.sha256(secret).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(_fernet_key())


def encrypt_llm_key(plain: str) -> str:
    """Encrypt a plaintext API key for storage. Never call this on anything
    you intend to log."""
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_llm_key(token: str) -> str | None:
    """Decrypt a stored key. Returns None (and logs, without the plaintext)
    on any failure — a corrupt/rotated-key row degrades to "no key" instead
    of crashing the request."""
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        logger.warning("llm_key_decrypt_failed")
        return None


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
# Analysis
# ----------------------------------------------------------------------------


def get_analysis(paper: Paper, *, target_lang: str = "tr") -> PaperAnalysis | None:
    """Cache-only lookup. Returns None if no analysis exists yet."""
    return PaperAnalysis.query.filter_by(paper_id=paper.id, target_lang=target_lang).first()


def generate_analysis(paper: Paper, *, target_lang: str = "tr", user=None) -> PaperAnalysis | None:
    """Force an LLM call (routed via `_call_llm` for `user`'s resolved
    provider) and upsert the cache row. Returns None on failure."""
    if not is_ai_enabled(user):
        return None
    if target_lang != "tr":
        # Only TR analysis is wired up for now. Other locales fall back to
        # the dev's preferred language when the user explicitly asks.
        target_lang = "tr"

    title = (paper.title or "").strip()
    abstract = (paper.abstract or "").strip()
    user_msg = f"Makale başlığı:\n{title}\n\nÖzet:\n{abstract or '(özet yok)'}"

    parsed, raw = _call_llm(
        system=_ANALYSIS_SYSTEM_TR, user_msg=user_msg, max_tokens=MAX_TOKENS_ANALYSIS, user=user
    )
    if parsed is None:
        return None

    existing = get_analysis(paper, target_lang=target_lang)
    fields = dict(
        tldr=_safe_str(parsed.get("tldr")),
        method=_safe_list(parsed.get("method")),
        findings=_safe_list(parsed.get("findings")),
        limitations=_safe_list(parsed.get("limitations")),
        personal_relevance=_safe_str(parsed.get("personal_relevance")),
        model_version=_model_label(user),
        raw_response={"text": raw} if isinstance(raw, str) else None,
    )
    if existing is None:
        analysis = PaperAnalysis(paper_id=paper.id, target_lang=target_lang, **fields)
        db.session.add(analysis)
    else:
        for k, v in fields.items():
            setattr(existing, k, v)
        analysis = existing
    db.session.commit()
    logger.info("analysis_generated", paper_id=paper.id, model=_model_label(user))
    return analysis


def get_or_generate_analysis(
    paper: Paper, *, target_lang: str = "tr", force: bool = False, user=None
) -> PaperAnalysis | None:
    """Returns a cached analysis or generates one. None means AI is off or
    the call failed — caller renders the disabled / retry UI."""
    if not force:
        cached = get_analysis(paper, target_lang=target_lang)
        if cached is not None:
            return cached
    return generate_analysis(paper, target_lang=target_lang, user=user)


# ----------------------------------------------------------------------------
# Translation
# ----------------------------------------------------------------------------


def get_translation(paper: Paper, *, target_lang: str = "tr") -> PaperTranslation | None:
    return PaperTranslation.query.filter_by(paper_id=paper.id, target_lang=target_lang).first()


def generate_translation(
    paper: Paper, *, target_lang: str = "tr", user=None
) -> PaperTranslation | None:
    if not is_ai_enabled(user):
        return None
    title = (paper.title or "").strip()
    abstract = (paper.abstract or "").strip()
    if not title and not abstract:
        return None
    user_msg = f"Başlık:\n{title}\n\nÖzet:\n{abstract or '(özet yok)'}"

    parsed, _raw = _call_llm(
        system=_TRANSLATION_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=MAX_TOKENS_TRANSLATION,
        user=user,
    )
    if parsed is None:
        return None

    existing = get_translation(paper, target_lang=target_lang)
    fields = dict(
        title_translated=_safe_str(parsed.get("title_translated")),
        abstract_translated=_safe_str(parsed.get("abstract_translated")),
        model_version=_model_label(user),
    )
    if existing is None:
        tr = PaperTranslation(paper_id=paper.id, target_lang=target_lang, **fields)
        db.session.add(tr)
    else:
        for k, v in fields.items():
            setattr(existing, k, v)
        tr = existing
    db.session.commit()
    logger.info("translation_generated", paper_id=paper.id, model=_model_label(user))
    return tr


def get_or_generate_translation(
    paper: Paper, *, target_lang: str = "tr", force: bool = False, user=None
) -> PaperTranslation | None:
    if not force:
        cached = get_translation(paper, target_lang=target_lang)
        if cached is not None:
            return cached
    return generate_translation(paper, target_lang=target_lang, user=user)


# ----------------------------------------------------------------------------
# Video summary (Faz 3 — agent reach: channel ingestion + transcript
# summarization). One row per paper — see VideoSummary's docstring for why
# this cache is unique on paper_id alone, unlike PaperAnalysis/PaperTranslation.
# ----------------------------------------------------------------------------


def get_video_summary(paper: Paper) -> VideoSummary | None:
    """Cache-only lookup. Returns None if no summary exists yet."""
    return VideoSummary.query.filter_by(paper_id=paper.id).first()


def generate_video_summary(
    paper: Paper,
    transcript: str | None,
    *,
    target_lang: str = "tr",
    source_lang: str | None = None,
    user=None,
) -> VideoSummary | None:
    """Force an LLM call over `transcript` (routed via `_call_llm` for
    `user`'s resolved provider) and upsert the cache row. Returns None on
    failure, a blank transcript, or when AI is disabled — never persists a
    partial row."""
    if not is_ai_enabled(user):
        return None

    transcript = (transcript or "").strip()
    if not transcript:
        return None

    title = (paper.title or "").strip()
    user_msg = (
        f"Video başlığı:\n{title}\n\n"
        f"Transkript (otomatik oluşturulmuş, noktalama eksik olabilir):\n"
        f"{_truncate(transcript, VIDEO_TRANSCRIPT_PROMPT_CHARS)}"
    )

    parsed, raw = _call_llm(
        system=_VIDEO_SUMMARY_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=MAX_TOKENS_VIDEO_SUMMARY,
        user=user,
    )
    if parsed is None:
        return None

    existing = get_video_summary(paper)
    fields = dict(
        tldr=_safe_str(parsed.get("tldr")),
        highlights=_safe_list(parsed.get("highlights")),
        topics=_safe_list(parsed.get("topics")),
        transcript_chars=len(transcript),
        source_lang=source_lang,
        target_lang=target_lang,
        model_version=_model_label(user),
        raw_response={"text": raw} if isinstance(raw, str) else None,
    )
    if existing is None:
        summary = VideoSummary(paper_id=paper.id, **fields)
        db.session.add(summary)
    else:
        for k, v in fields.items():
            setattr(existing, k, v)
        summary = existing
    db.session.commit()
    logger.info("video_summary_generated", paper_id=paper.id, model=_model_label(user))
    return summary


def get_or_generate_video_summary(
    paper: Paper,
    transcript: str | None = None,
    *,
    force: bool = False,
    user=None,
) -> VideoSummary | None:
    """Returns a cached summary or generates one. None means AI is off, the
    call failed, or (on a cache miss) no transcript was supplied."""
    if not force:
        cached = get_video_summary(paper)
        if cached is not None:
            return cached
    return generate_video_summary(paper, transcript, user=user)


def ask_paper(paper: Paper, question: str, history: list[dict] = None, *, user=None) -> str | None:
    """Ask a question about a paper using the resolved LLM, with abstract and
    notes context.

    history is a list of dicts: [{'role': 'user'|'assistant', 'content': '...'}]
    """
    if not is_ai_enabled(user):
        return None

    title = (paper.title or "").strip()
    abstract = (paper.abstract or "").strip()

    from app.modules.scrape.models import UserPaper

    notes_text = ""
    if user is not None:
        user_paper = UserPaper.query.filter_by(user_id=user.id, paper_id=paper.id).first()
        if user_paper and user_paper.notes:
            notes_text = "\n\nKullanıcının bu makale üzerine aldığı notlar:\n" + "\n".join(
                f"- [{n.tag or 'Not'}]: {n.body}" for n in user_paper.notes
            )

    system_prompt = f"""Sen akademik bir araştırma asistanısın. Sana başlığı ve özeti verilen şu bilimsel makale hakkında soruları yanıtlayacaksın.
Cevaplarını DAİMA samimi, net, markdown formatında ve Türkçe olarak ver. Eğer yanıt makalede yer almıyorsa, bunu açıkça belirt.

Makale Başlığı: {title}
Makale Özeti:
{abstract}{notes_text}"""

    convo = ""
    if history:
        lines = []
        for msg in history:
            role_label = "Kullanıcı" if msg.get("role") == "user" else "Asistan"
            lines.append(f"{role_label}: {msg.get('content', '')}")
        convo = "\n".join(lines) + "\n\n"
    user_msg = f"{convo}Kullanıcı: {question}"

    answer, _raw = _call_llm(
        system=system_prompt,
        user_msg=user_msg,
        max_tokens=MAX_TOKENS_CHAT,
        user=user,
        expect_json=False,
    )
    if answer is None:
        return None
    return answer.strip() or None


MAX_TOKENS_WEB_SUMMARY = 300

#: Enough of the page for the model to know what it is; the rest is almost
#: always navigation, footers and repeated boilerplate.
_WEB_SUMMARY_INPUT_CHARS = 6000


def summarize_web_content(title: str, content: str, *, user=None) -> str | None:
    """Two-or-three sentence summary of a fetched web page.

    Used by the manual "add link" flow, where the reader output still opens
    on chrome (language switchers, badges, install banners) that no
    deterministic cleaner can recognise as noise. Returns None when AI is
    unavailable so the caller keeps its cleaned-text fallback — a missing
    summary must never block saving the link.
    """
    body = (content or "").strip()
    if not body:
        return None

    system_prompt = (
        "Sen bir araştırma asistanısın. Sana bir web sayfasının başlığı ve "
        "metni verilecek. Sayfanın ne hakkında olduğunu 2-3 cümleyle, düz "
        "metin olarak özetle. Menü, dil seçici, rozet ve kurulum talimatı "
        "gibi içerikle ilgisiz kısımları yok say. Başlığı tekrar etme, "
        "madde işareti veya markdown kullanma."
    )
    user_msg = f"Başlık: {title}\n\nSayfa metni:\n{body[:_WEB_SUMMARY_INPUT_CHARS]}"

    answer, _raw = _call_llm(
        system=system_prompt,
        user_msg=user_msg,
        max_tokens=MAX_TOKENS_WEB_SUMMARY,
        user=user,
        expect_json=False,
    )
    if answer is None:
        return None
    return answer.strip() or None


_NOVELTY_SYSTEM_TR = (
    "Sen bir patent ön araştırma asistanısın. Kullanıcı bir buluş fikrini "
    "anlatacak, ardından bu fikirle ilgili olabilecek yayınlanmış patentlerin "
    "listesini vereceğim. Görevin, fikrin bu patentler karşısında ne kadar "
    "yeni göründüğünü değerlendirmek.\n\n"
    "Kurallar:\n"
    "- Sadece sana verilen patent listesine dayan. Listede olmayan bir "
    "patenti varmış gibi anma.\n"
    "- En yakın patentleri numaralarıyla göster ve neden yakın olduklarını yaz.\n"
    "- Hukuki tavsiye verme. Bu bir patentlenebilirlik görüşü değil, bir "
    "okuma yardımıdır; sonuç mutlaka bir patent vekiliyle doğrulanmalıdır.\n"
    "- Emin değilsen bunu açıkça söyle.\n\n"
    'Yanıtı şu JSON şemasıyla ver: {"verdict": "...", "confidence": '
    '"low|medium|high", "closest": [{"id": "...", "why": "..."}], '
    '"differentiators": ["..."], "caveats": "..."}\n'
    '"verdict" 2-3 cümlelik düz Türkçe bir değerlendirme olsun. '
    '"differentiators" fikri listeden ayıran noktalar; hiçbiri yoksa boş '
    "liste ver — uydurma."
)


def analyze_novelty(idea: str, patents: list, *, user=None) -> dict | None:
    """LLM assessment of how novel `idea` looks against `patents`.

    `patents` is a list of `PaperPayload` (the live prior-art search never
    persists — see `service.search_patents_live`), so this reads payload
    attributes, not model columns.

    Returns the parsed JSON dict, or None when AI is unavailable or the model
    did not return usable JSON. None is a normal outcome: the page still shows
    the patent list, which is the part with actual evidentiary value. The
    assessment is a reading aid layered on top.

    Deliberately **not** cached. Prior-art queries are one-off and the answer
    depends on the exact wording of the idea; a cache keyed on anything less
    than the full text would hand back an assessment of a different question.
    """
    idea = (idea or "").strip()
    if not idea or not patents:
        return None

    lines = []
    for p in patents[:NOVELTY_MAX_ITEMS]:
        assignees = [
            c[len("assignee:") :]
            for c in (getattr(p, "categories", None) or [])
            if isinstance(c, str) and c.startswith("assignee:")
        ]
        year = getattr(getattr(p, "published_at", None), "year", None)
        owner = f" · {assignees[0]}" if assignees else ""
        lines.append(
            f"- [{p.external_id}] {p.title} ({year or 'tarih yok'}){owner}\n"
            f"  {_truncate(p.abstract, NOVELTY_ABSTRACT_CHARS) or 'Özet yok.'}"
        )

    user_msg = "Buluş fikri:\n" + idea + "\n\nİlgili patentler:\n" + "\n".join(lines)

    parsed, _raw = _call_llm(
        system=_NOVELTY_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=MAX_TOKENS_NOVELTY,
        user=user,
    )
    if not isinstance(parsed, dict):
        return None
    return parsed


# ----------------------------------------------------------------------------
# Retrospective report (Faz 6) — map/reduce summarization over a report's
# item set. Aggregation + the numeric `stats` backbone are report_service.py's
# job (a parallel module); this file only ever sees one chunk's items (map)
# or the collected chunk summaries + stats (reduce).
# ----------------------------------------------------------------------------


def summarize_report_chunk(items: list[dict], *, context: str, user=None) -> dict | None:
    """Map step: summarize one chunk of a retrospective report's item set.

    `items` is a list of dicts shaped like::

        {"title": str, "abstract": str | None, "year": int | None,
         "cited_by_count": int | None, "ref": int}

    `ref` is a 1-based index the *caller* assigns to identify this item
    within the chunk (not necessarily contiguous with other chunks — each
    call to this function is self-contained) — it is echoed back in
    `notable[].ref` so the caller can resolve it back to a concrete row.
    `title`/`abstract`/`year`/`cited_by_count` are read defensively (missing
    or None is fine); `abstract` is truncated to `REPORT_ABSTRACT_CHARS`
    before it goes into the prompt, same cost-guard reasoning as
    `DIGEST_MAX_ITEMS`/`NOVELTY_ABSTRACT_CHARS` elsewhere in this module.

    `context` is a short prose description of what this chunk represents,
    e.g. ``"2023 yılı, en çok atıf alan 25 çalışma"`` — folded into the
    prompt so the model knows what slice of the retrospective it is reading.

    Returns a dict shaped like::

        {"themes": [str, ...],
         "notable": [{"ref": int, "why": str}, ...],
         "methods": [str, ...]}

    or `None` when AI is disabled, `items` has no item with a usable `ref`,
    or the LLM call/parse fails (after one repair retry, same "model
    returned a scalar instead of an object" pattern as `generate_digest`).
    A `None` result must not fail the whole report — map/reduce is designed
    so one bad chunk just means a thinner reduce step, not no report at
    all — but enforcing that is the caller's job (report_service.py), not
    this function's.

    `ref` validation mirrors `generate_digest`'s highlight-ref resolution:
    the model can hallucinate an index that was never given to it, so any
    `notable[].ref` outside the set of refs actually present in `items` is
    dropped rather than trusted.
    """
    if not is_ai_enabled(user):
        return None
    if not items:
        return None

    valid_refs: set[int] = set()
    lines = []
    for item in items:
        try:
            ref = int(item.get("ref"))
        except (TypeError, ValueError):
            continue
        valid_refs.add(ref)
        title = (item.get("title") or "").strip()
        abstract = _truncate(item.get("abstract"), REPORT_ABSTRACT_CHARS)
        year = item.get("year")
        cited = item.get("cited_by_count")
        lines.append(
            f"{ref}. Başlık: {title}\n"
            f"   Yıl: {year if year is not None else 'bilinmiyor'}\n"
            f"   Atıf sayısı: {cited if cited is not None else 'bilinmiyor'}\n"
            f"   Özet: {abstract or '(özet yok)'}"
        )
    if not valid_refs:
        return None

    user_msg = f"Bağlam: {context}\n\nÇalışmalar:\n\n" + "\n\n".join(lines)

    parsed, _raw = _call_llm(
        system=_REPORT_MAP_SYSTEM_TR, user_msg=user_msg, max_tokens=MAX_TOKENS_REPORT_MAP, user=user
    )
    # Same one-shot repair retry as generate_digest/score_feed_relevance:
    # small/free models occasionally emit a JSON scalar or bare list instead
    # of the object schema.
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
            "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
            "yalnızca şemadaki nesneyi ver."
        )
        parsed, _raw = _call_llm(
            system=_REPORT_MAP_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=MAX_TOKENS_REPORT_MAP,
            user=user,
        )
    if not isinstance(parsed, dict):
        logger.warning("report_chunk_parsed_not_object", user_id=getattr(user, "id", None))
        return None

    notable_raw = parsed.get("notable") or []
    notable: list[dict] = []
    if isinstance(notable_raw, list):
        for n in notable_raw:
            if not isinstance(n, dict):
                continue
            try:
                ref_idx = int(n.get("ref"))
            except (TypeError, ValueError):
                continue
            if ref_idx not in valid_refs:
                continue
            why = _safe_str(n.get("why"))
            if why is None:
                continue
            notable.append({"ref": ref_idx, "why": why})

    result = {
        "themes": _safe_list(parsed.get("themes")) or [],
        "notable": notable,
        "methods": _safe_list(parsed.get("methods")) or [],
    }
    logger.info(
        "report_chunk_summarized",
        user_id=getattr(user, "id", None),
        items=len(valid_refs),
        notable=len(notable),
    )
    return result


_AUTHOR_GROUP_REDUCE_SYSTEM_TR = """Sen bir araştırma-istihbarat asistanısın. Bir kullanıcı, adını kendisi verdiği bir yazar grubunun (bir ekip, bir komite, bir laboratuvar — grubun ne olduğunu bilmiyorsun ve varsaymamalısın) geçmiş yayın üretimi hakkında bir dosya istiyor. Sana iki girdi verilecek: (1) LLM kullanılmadan hesaplanmış sayısal bir özet (üye başına yayın sayısı, yıl dağılımı, mekânlar, konular, üyeler arası ortak yazarlık bağları, kullanıcının kendi anahtar kelimeleriyle kesişim) ve (2) üye üye, o üyenin çalışmalarından çıkarılmış parça özetleri.

Görevin: her üyenin **neyle uğraştığını** kendi cümlelerinle anlatmak, grubun ortak zeminini bulmak ve bunun kullanıcının kendi çalışma alanıyla nerede kesiştiğini söylemek.

Kurallar:
- Yalnızca sana verilen veriye dayan. Bir üye hakkında veri yoksa onu uydurma.
- Kişiler hakkında değerlendirici/kişisel yorum yapma (iyi/kötü, güçlü/zayıf   araştırmacı gibi). Yalnızca çalışmalarının konusunu ve yöntemini tarif et.
- "focus" bir etiket listesi değil, 1-2 cümlelik okunabilir bir tarif olsun.

Cevabını DAİMA Türkçe ver ve şu JSON şemasına uygun döndür:

{
  "tldr": "2-4 cümle: bu grup topluca hangi alanlarda çalışıyor",
  "members": [
    {"name": "üyenin adı (sana verilen adla birebir aynı)",
     "focus": "1-2 cümle: bu kişinin çalışmalarının konusu ve yöntemi",
     "recent_highlights": ["öne çıkan çalışma hakkında kısa not", "..."]}
  ],
  "shared_topics": ["birden fazla üyenin ortaklaştığı konu", "..."],
  "overlap_with_you": "kullanıcının anahtar kelimeleriyle kesişim; kesişim yoksa bunu açıkça söyle",
  "coauthorship_notes": "üyeler arasında ortak yazarlık var mı, hangi konularda; yoksa bunu söyle"
}"""


def synthesize_report(chunk_summaries: list[dict], stats: dict, *, user=None) -> dict | None:
    """Reduce step: combine every chunk's `summarize_report_chunk` output
    with a numeric backbone into the final retrospective report.

    `chunk_summaries` is a list of dicts, each shaped like
    `summarize_report_chunk`'s return value (`{"themes": [...], "notable":
    [...], "methods": [...]}`). A chunk that failed to summarize returns
    `None` from that function — filter those out *before* calling this one;
    passing a `None` entry here is tolerated (skipped) but the caller owns
    deciding whether "some chunks failed" should still produce a report.

    `stats` is the report's numeric backbone, computed without any LLM call
    (year-by-year counts, top authors/venues, topic mix, OA ratio, ...).
    This function makes no assumption about its shape — it is serialized
    as-is (indented JSON) into the prompt so the model can read whatever the
    caller computed.

    Returns a dict shaped exactly like `Report.sections`::

        {"tldr": str,
         "timeline": [{"year": int, "themes": [str], "notable": [str]}],
         "emerging": [str], "fading": [str],
         "key_works": [{"title": str, "why": str}],
         "key_authors": [str], "key_venues": [str],
         "for_your_keywords": str}

    Returns `None` when AI is disabled, both `chunk_summaries` and `stats`
    are empty (nothing to synthesize), or the LLM call/parse fails (after
    one repair retry). Never raises.
    """
    if not is_ai_enabled(user):
        return None
    if not chunk_summaries and not stats:
        return None

    stats_json = json.dumps(stats, ensure_ascii=False, indent=2, default=str)
    chunk_blocks = [
        json.dumps(cs, ensure_ascii=False, indent=2)
        for cs in chunk_summaries
        if isinstance(cs, dict)
    ]
    chunks_text = "\n\n".join(
        f"Bölüm özeti {i}:\n{block}" for i, block in enumerate(chunk_blocks, start=1)
    )

    user_msg = (
        "Sayısal özet (LLM kullanılmadan hesaplanmış):\n"
        + stats_json
        + "\n\nBölüm özetleri (map adımından):\n\n"
        + (chunks_text or "(bölüm özeti yok)")
    )

    parsed, _raw = _call_llm(
        system=_REPORT_REDUCE_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=MAX_TOKENS_REPORT_REDUCE,
        user=user,
    )
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
            "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
            "yalnızca şemadaki nesneyi ver."
        )
        parsed, _raw = _call_llm(
            system=_REPORT_REDUCE_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=MAX_TOKENS_REPORT_REDUCE,
            user=user,
        )
    if not isinstance(parsed, dict):
        logger.warning("report_synthesis_parsed_not_object", user_id=getattr(user, "id", None))
        return None

    timeline_raw = parsed.get("timeline") or []
    timeline: list[dict] = []
    if isinstance(timeline_raw, list):
        for t in timeline_raw:
            if not isinstance(t, dict):
                continue
            try:
                year = int(t.get("year"))
            except (TypeError, ValueError):
                continue
            timeline.append(
                {
                    "year": year,
                    "themes": _safe_list(t.get("themes")) or [],
                    "notable": _safe_list(t.get("notable")) or [],
                }
            )

    key_works_raw = parsed.get("key_works") or []
    key_works: list[dict] = []
    if isinstance(key_works_raw, list):
        for kw in key_works_raw:
            if not isinstance(kw, dict):
                continue
            title = _safe_str(kw.get("title"))
            why = _safe_str(kw.get("why"))
            if not title or not why:
                continue
            key_works.append({"title": title, "why": why})

    result = {
        "tldr": _safe_str(parsed.get("tldr")) or "",
        "timeline": timeline,
        "emerging": _safe_list(parsed.get("emerging")) or [],
        "fading": _safe_list(parsed.get("fading")) or [],
        "key_works": key_works,
        "key_authors": _safe_list(parsed.get("key_authors")) or [],
        "key_venues": _safe_list(parsed.get("key_venues")) or [],
        "for_your_keywords": _safe_str(parsed.get("for_your_keywords")) or "",
    }
    logger.info(
        "report_synthesized",
        user_id=getattr(user, "id", None),
        chunk_count=len(chunk_blocks),
        timeline_years=len(timeline),
    )
    return result


def synthesize_author_group_report(
    member_summaries: dict[str, list[dict]], stats: dict, *, user=None
) -> dict | None:
    """Reduce step for an author-group dossier.

    Separate from `synthesize_report` rather than a `kind` flag on it because
    the two answer different questions. The topic report asks "what happened
    in this field over N years" and its schema is organised by time
    (`timeline`/`emerging`/`fading`). A group dossier asks "what do these
    specific people work on", and its schema is organised by person. Reusing
    the topic schema forced the caller to rebuild a per-member section out of
    raw map-phase theme fragments joined with semicolons — the model never
    got to write a characterisation of anyone, which is the one thing this
    report exists to produce.

    `member_summaries` maps a member's display name to that member's
    `summarize_report_chunk` outputs. The name is echoed back in
    `members[].name`; anything returned under a name that was not supplied is
    dropped — the same "the model can invent an identifier" defence
    `summarize_report_chunk` applies to `ref`, and a dossier attributing work
    to someone who was never in the group is worse than a thinner one.

    The prompt forbids evaluative judgements about people. This report is
    assembled from a named individual's publication record, so "describe the
    work" is in scope and "rate the researcher" is not.

    Returns `None` when AI is disabled, there is nothing to synthesize, or
    the call/parse fails after one repair retry. Never raises; the caller
    keeps showing the deterministic `stats` either way.
    """
    if not is_ai_enabled(user):
        return None
    if not member_summaries and not stats:
        return None

    known_names = {n for n in member_summaries if isinstance(n, str) and n.strip()}

    stats_json = json.dumps(stats, ensure_ascii=False, indent=2, default=str)
    blocks = []
    for name, chunks in member_summaries.items():
        usable = [c for c in (chunks or []) if isinstance(c, dict)]
        if not usable:
            continue
        body = json.dumps(usable, ensure_ascii=False, indent=2)
        blocks.append("Uye: " + str(name) + "\n" + body)

    user_msg = (
        "Sayisal ozet (LLM kullanilmadan hesaplanmis):\n"
        + stats_json
        + "\n\nUye bazinda parca ozetleri:\n\n"
        + ("\n\n".join(blocks) or "(uye ozeti yok)")
    )

    parsed, _raw = _call_llm(
        system=_AUTHOR_GROUP_REDUCE_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=MAX_TOKENS_REPORT_REDUCE,
        user=user,
    )
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nONEMLI: Yanitin DOGRUDAN gecerli bir JSON NESNESI olmali - '{' ile "
            "baslayip '}' ile bitmeli. Metin, aciklama, string veya liste dondurme; "
            "yalnizca semadaki nesneyi ver."
        )
        parsed, _raw = _call_llm(
            system=_AUTHOR_GROUP_REDUCE_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=MAX_TOKENS_REPORT_REDUCE,
            user=user,
        )
    if not isinstance(parsed, dict):
        logger.warning(
            "author_group_synthesis_parsed_not_object", user_id=getattr(user, "id", None)
        )
        return None

    members: list[dict] = []
    for m in parsed.get("members") or []:
        if not isinstance(m, dict):
            continue
        name = _safe_str(m.get("name"))
        if not name or (known_names and name not in known_names):
            continue
        members.append(
            {
                "name": name,
                "focus": _safe_str(m.get("focus")) or "",
                "recent_highlights": _safe_list(m.get("recent_highlights")) or [],
            }
        )

    result = {
        "tldr": _safe_str(parsed.get("tldr")) or "",
        "members": members,
        "shared_topics": _safe_list(parsed.get("shared_topics")) or [],
        "overlap_with_you": _safe_str(parsed.get("overlap_with_you")) or "",
        "coauthorship_notes": _safe_str(parsed.get("coauthorship_notes")) or "",
    }
    logger.info(
        "author_group_report_synthesized",
        user_id=getattr(user, "id", None),
        member_count=len(members),
    )
    return result


# ----------------------------------------------------------------------------
# Digest (Bölüm B) — daily/weekly LLM briefing over newly-surfaced papers
# ----------------------------------------------------------------------------


def _truncate(text: str | None, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def generate_digest(
    user,
    links: list,
    *,
    period: str,
    period_start,
    period_end,
    target_lang: str = "tr",
) -> UserDigest | None:
    """Summarise a window of newly-surfaced `UserPaper` links for `user` via
    the resolved LLM. Upserts a `UserDigest` row keyed on
    (user, period, period_start) — idempotent re-runs (same window) overwrite
    in place instead of piling up duplicates.

    `period_start`/`period_end` are the caller's window bounds (computed by
    `digest_tasks.run_for_user` from `period`) — passed in explicitly rather
    than re-derived here so the upsert key is stable across retries within
    the same task run.

    Returns None (and never persists a partial row) when:
      * AI is disabled for this user (no key resolvable)
      * `links` is empty (no point calling the LLM for nothing)
      * the LLM call fails / returns unparsable output
    """
    if not is_ai_enabled(user):
        return None
    if not links:
        return None

    window = links[:DIGEST_MAX_ITEMS]
    lines = []
    for idx, link in enumerate(window, start=1):
        paper = link.paper
        title = (paper.title or "").strip()
        abstract = _truncate(paper.abstract, 500)
        kw = link.matched_keyword or ""
        lines.append(
            f"{idx}. Başlık: {title}\n   İlgili anahtar kelime: {kw}\n   Özet: {abstract or '(özet yok)'}"
        )
    user_msg = "Aşağıdaki makaleleri özetle:\n\n" + "\n\n".join(lines)

    parsed, raw = _call_llm(
        system=_DIGEST_SYSTEM_TR, user_msg=user_msg, max_tokens=MAX_TOKENS_DIGEST, user=user
    )
    # Small/weak models sometimes emit a JSON scalar (a bare string or list)
    # instead of the object schema. Retry once with a firmer object-only
    # instruction before giving up — cheap insurance for local/free models.
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
            "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
            "yalnızca şemadaki nesneyi ver."
        )
        parsed, raw = _call_llm(
            system=_DIGEST_SYSTEM_TR, user_msg=retry_msg, max_tokens=MAX_TOKENS_DIGEST, user=user
        )
    # Still not a usable object → failed run, no partial row persisted.
    if not isinstance(parsed, dict):
        logger.warning("digest_parsed_not_object", user_id=getattr(user, "id", None))
        return None

    # Resolve "ref" indices (1-based, matching the numbered list above) back
    # to real user_paper ids so the template can link straight to /papers/<id>.
    highlights_raw = parsed.get("highlights") or []
    highlights: list[dict] = []
    if isinstance(highlights_raw, list):
        for h in highlights_raw:
            if not isinstance(h, dict):
                continue
            ref = h.get("ref")
            user_paper_id = None
            try:
                ref_idx = int(ref)
                if 1 <= ref_idx <= len(window):
                    user_paper_id = window[ref_idx - 1].id
            except (TypeError, ValueError):
                user_paper_id = None
            entry = {
                "title": _safe_str(h.get("title")) or "",
                "why": _safe_str(h.get("why")) or "",
                "user_paper_id": user_paper_id,
            }
            highlights.append(entry)

    fields = dict(
        period_end=period_end,
        summary=_safe_str(parsed.get("summary")),
        highlights=highlights or None,
        themes=_safe_list(parsed.get("themes")),
        item_count=len(window),
        model_version=_model_label(user),
        raw_response={"text": raw} if isinstance(raw, str) else None,
    )

    existing = UserDigest.query.filter_by(
        user_id=user.id, period=period, period_start=period_start
    ).first()
    if existing is None:
        digest = UserDigest(user_id=user.id, period=period, period_start=period_start, **fields)
        db.session.add(digest)
    else:
        for k, v in fields.items():
            setattr(existing, k, v)
        digest = existing
    db.session.commit()
    logger.info(
        "digest_generated",
        user_id=user.id,
        period=period,
        item_count=len(window),
        model=_model_label(user),
    )
    return digest


# ----------------------------------------------------------------------------
# Feed relevance scoring (Faz 2 Bölüm D) — batch-score industry announcements
# ----------------------------------------------------------------------------


def score_feed_relevance(user, papers: list) -> list[dict]:
    """Score a batch of `Paper` rows (expected `kind="news"`) for relevance to
    `user`'s interest keywords, via ONE `_call_llm` call — never one call per
    paper. Returns a list of `{paper_id, score, why, matched_keyword}` dicts,
    one per paper the model scored; an empty list means "nothing to link"
    (AI disabled, no keywords, empty input, or an unparsable LLM response) —
    callers must treat that the same as "score everything below threshold",
    never as an error to surface.

    Reuses the exact dict-guard + single repair-retry pattern from
    `generate_digest`: small/free models occasionally return a JSON scalar or
    a bare list instead of the `{"scores": [...]}` object schema, and must
    not crash `_link_relevant_feed_items`'s per-item lookups.
    """
    if not is_ai_enabled(user):
        return []
    if not papers:
        return []

    from app.modules.academic.service import list_user_keywords

    keywords = [kw.value for kw in list_user_keywords(user)]
    if not keywords:
        return []

    window = papers[:FEED_SCORE_MAX_ITEMS]
    lines = []
    for idx, paper in enumerate(window, start=1):
        title = (paper.title or "").strip()
        abstract = _truncate(paper.abstract, 400)
        lines.append(f"{idx}. Başlık: {title}\n   Özet: {abstract or '(özet yok)'}")
    user_msg = (
        "Kullanıcının ilgi alanı anahtar kelimeleri: "
        + ", ".join(keywords)
        + "\n\nDuyurular:\n\n"
        + "\n\n".join(lines)
    )

    parsed, _raw = _call_llm(
        system=_FEED_SCORE_SYSTEM_TR, user_msg=user_msg, max_tokens=MAX_TOKENS_FEED_SCORE, user=user
    )
    # Small/weak models sometimes emit a JSON scalar or bare list instead of
    # the object schema — same one-shot repair retry as generate_digest.
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
            "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
            "yalnızca şemadaki nesneyi ver."
        )
        parsed, _raw = _call_llm(
            system=_FEED_SCORE_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=MAX_TOKENS_FEED_SCORE,
            user=user,
        )
    if not isinstance(parsed, dict):
        logger.warning("feed_score_parsed_not_object", user_id=getattr(user, "id", None))
        return []

    scores_raw = parsed.get("scores")
    if not isinstance(scores_raw, list):
        return []

    out: list[dict] = []
    for item in scores_raw:
        if not isinstance(item, dict):
            continue
        try:
            ref_idx = int(item.get("ref"))
        except (TypeError, ValueError):
            continue
        if not (1 <= ref_idx <= len(window)):
            continue
        try:
            score = int(item.get("score"))
        except (TypeError, ValueError):
            continue
        score = max(0, min(100, score))
        out.append(
            {
                "paper_id": window[ref_idx - 1].id,
                "score": score,
                "why": _safe_str(item.get("why")) or "",
                "matched_keyword": _safe_str(item.get("matched_keyword")),
            }
        )
    logger.info(
        "feed_relevance_scored",
        user_id=getattr(user, "id", None),
        scored=len(out),
        window=len(window),
    )
    return out


# ----------------------------------------------------------------------------
# Interest → topic classification (Faz 3 Bölüm B) — lexicon fast-path + LLM
# fallback for the source picker's "suggested for you" grouping/defaults.
# ----------------------------------------------------------------------------

# Cheap TR/EN substring lexicon, checked with word-boundary regex (so short
# terms like "ai" don't false-match inside "explain"/"maintain"). This covers
# the vast majority of real interest keywords for ~0 cost; only whatever it
# can't resolve goes to the one-shot LLM call below. Not meant to be
# exhaustive — easy to extend, and wrong/missing entries just fall through to
# the LLM instead of being a hard failure.
_TOPIC_LEXICON: dict[str, list[str]] = {
    "ai": [
        "yapay zeka",
        "yapay zekası",
        "ai",
        "artificial intelligence",
        "llm",
        "büyük dil modeli",
        "large language model",
        "genai",
        "generative ai",
        "üretken yapay zeka",
        "chatbot",
        "agent",
        "ajan",
    ],
    "ml": [
        "makine öğrenmesi",
        "makine ogrenmesi",
        "machine learning",
        "deep learning",
        "derin öğrenme",
        "derin ogrenme",
        "neural network",
        "sinir ağı",
        "sinir agi",
        "reinforcement learning",
        "pekiştirmeli öğrenme",
        "transformer",
        "transformers",
        "nlp",
        "doğal dil işleme",
        "dogal dil isleme",
        "computer vision",
        "görüntü işleme",
        "goruntu isleme",
    ],
    "cs": [
        "bilgisayar bilimi",
        "computer science",
        "algoritma",
        "algorithm",
        "yazılım",
        "yazilim",
        "software",
        "programlama",
        "programming",
        "dağıtık sistemler",
        "dagitik sistemler",
        "distributed systems",
        "veritabanı",
        "veritabani",
        "database",
        "siber güvenlik",
        "cybersecurity",
        "security",
    ],
    "physics": [
        "fizik",
        "physics",
        "kuantum",
        "quantum",
        "parçacık fiziği",
        "parcacik fizigi",
        "particle physics",
        "astrofizik",
        "astrophysics",
        "kozmoloji",
        "cosmology",
    ],
    "math": [
        "matematik",
        "mathematics",
        "istatistik",
        "statistics",
        "olasılık",
        "olasilik",
        "probability",
        "cebir",
        "algebra",
        "topoloji",
        "topology",
    ],
    "biomed": [
        "biyoloji",
        "biology",
        "genetik",
        "genetics",
        "tıp",
        "tip",
        "medicine",
        "biyomedikal",
        "biomedical",
        "sağlık",
        "saglik",
        "health",
        "nörobilim",
        "norobilim",
        "neuroscience",
        "kanser",
        "cancer",
        "immünoloji",
        "immunology",
    ],
    "social": [
        "sosyoloji",
        "sociology",
        "ekonomi",
        "economics",
        "psikoloji",
        "psychology",
        "siyaset",
        "siyaset bilimi",
        "political science",
        "antropoloji",
        "anthropology",
        "eğitim bilimleri",
        "egitim bilimleri",
        "education",
    ],
    "humanities": [
        "tarih",
        "history",
        "felsefe",
        "philosophy",
        "edebiyat",
        "literature",
        "sanat tarihi",
        "art history",
        "dilbilim",
        "linguistics",
        "din",
        "religion",
        "arkeoloji",
        "archaeology",
    ],
}


def _topics_hash(keywords: list[str]) -> str:
    """Stable hash of the user's current keyword set — used to invalidate the
    cached topic classification only when interests actually change."""
    import hashlib

    joined = "|".join(sorted(k.strip().lower() for k in keywords if k and k.strip()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _cached_user_topics(user) -> tuple[list[str] | None, str | None]:
    """(cached_topic_keys, cached_keyword_hash) from `UserSettings.settings["topics"]`,
    or (None, None) if nothing is cached yet."""
    settings_row = _user_settings_row(user)
    if settings_row is None or not settings_row.settings:
        return None, None
    data = (settings_row.settings or {}).get("topics") or {}
    keys = data.get("keys")
    kw_hash = data.get("hash")
    if isinstance(keys, list) and kw_hash:
        return keys, kw_hash
    return None, None


def _store_user_topics(user, keys: list[str], kw_hash: str) -> None:
    from app.core.models.settings import UserSettings

    settings_row = _user_settings_row(user)
    if settings_row is None:
        settings_row = UserSettings(user_id=user.id, settings={})
        db.session.add(settings_row)
    data = dict(settings_row.settings or {})
    data["topics"] = {"keys": keys, "hash": kw_hash}
    settings_row.settings = data
    db.session.commit()


def _lexicon_topics(keyword: str) -> set[str]:
    """Topic keys matched via `_TOPIC_LEXICON` for one keyword, using
    word-boundary matching so short terms (e.g. "ai") don't fire on
    unrelated substrings."""
    import re

    lowered = keyword.strip().lower()
    if not lowered:
        return set()
    matched: set[str] = set()
    for topic, terms in _TOPIC_LEXICON.items():
        for term in terms:
            if re.search(r"\b" + re.escape(term) + r"\b", lowered):
                matched.add(topic)
                break
    return matched


def classify_user_topics(user) -> list[str]:
    """Map this user's research-interest keywords to `sources.TOPICS` keys —
    drives the interest-aware source picker (suggested sources + topic-gated
    feed defaults, see service.user_enabled_sources).

    Cheap TR/EN lexicon fast-path first; any keyword the lexicon can't place
    goes into ONE `_call_llm` batch call (dict-guard + single repair-retry,
    same pattern as `generate_digest`/`score_feed_relevance`). The result is
    cached in `UserSettings.settings["topics"]` alongside a hash of the
    keyword set, so repeat calls (e.g. every dashboard render) are free until
    the user's interests actually change.

    No interest keywords → `["general"]` (also cached, so this stays a cheap
    early-return on every subsequent call for that user).
    """
    from app.modules.academic.service import list_user_keywords

    keywords = [kw.value for kw in list_user_keywords(user)] if user is not None else []
    if not keywords:
        return ["general"]

    kw_hash = _topics_hash(keywords)
    cached_keys, cached_hash = _cached_user_topics(user)
    if cached_keys is not None and cached_hash == kw_hash:
        return cached_keys

    topics: set[str] = set()
    unresolved: list[str] = []
    for kw in keywords:
        matched = _lexicon_topics(kw)
        if matched:
            topics |= matched
        else:
            unresolved.append(kw)

    if unresolved:
        user_msg = "Anahtar kelimeler: " + ", ".join(unresolved)
        parsed, _raw = _call_llm(
            system=_TOPIC_CLASSIFY_SYSTEM_TR,
            user_msg=user_msg,
            max_tokens=MAX_TOKENS_TOPIC_CLASSIFY,
            user=user,
        )
        if not isinstance(parsed, dict):
            retry_msg = (
                user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
                "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
                "yalnızca şemadaki nesneyi ver."
            )
            parsed, _raw = _call_llm(
                system=_TOPIC_CLASSIFY_SYSTEM_TR,
                user_msg=retry_msg,
                max_tokens=MAX_TOKENS_TOPIC_CLASSIFY,
                user=user,
            )
        if isinstance(parsed, dict):
            llm_topics = parsed.get("topics")
            if isinstance(llm_topics, list):
                from app.modules.scrape.sources import TOPICS

                for t in llm_topics:
                    key = str(t).strip().lower()
                    if key in TOPICS:
                        topics.add(key)
        else:
            logger.warning("topic_classify_parsed_not_object", user_id=getattr(user, "id", None))

    result = sorted(topics) if topics else ["general"]
    _store_user_topics(user, result, kw_hash)
    logger.info(
        "user_topics_classified",
        user_id=getattr(user, "id", None),
        topics=result,
        unresolved=len(unresolved),
    )
    return result


# ----------------------------------------------------------------------------
# Keyword → English expansion (Faz 3 Bölüm E)
# ----------------------------------------------------------------------------

#: Common Turkish research terms and their canonical English form + synonyms.
#: Pure fast path — anything here never reaches the LLM. English terms map to
#: themselves so "machine learning" also short-circuits. Keys must be lowercase
#: and already normalised the way `_normalise_keyword` normalises user input.
_KEYWORD_EN_LEXICON: dict[str, tuple[str, list[str]]] = {
    "yapay zeka": ("artificial intelligence", ["AI"]),
    "yapay zekâ": ("artificial intelligence", ["AI"]),
    "artificial intelligence": ("artificial intelligence", ["AI"]),
    "makine öğrenmesi": ("machine learning", []),
    "makine ogrenmesi": ("machine learning", []),
    "machine learning": ("machine learning", []),
    "derin öğrenme": ("deep learning", []),
    "derin ogrenme": ("deep learning", []),
    "deep learning": ("deep learning", []),
    "büyük dil modeli": ("large language model", ["LLM"]),
    "buyuk dil modeli": ("large language model", ["LLM"]),
    "large language model": ("large language model", ["LLM"]),
    "sinir ağı": ("neural network", []),
    "sinir agi": ("neural network", []),
    "neural network": ("neural network", []),
    "doğal dil işleme": ("natural language processing", ["NLP"]),
    "dogal dil isleme": ("natural language processing", ["NLP"]),
    "bilgisayarlı görü": ("computer vision", []),
    "bilgisayarli goru": ("computer vision", []),
    "pekiştirmeli öğrenme": ("reinforcement learning", []),
    "pekistirmeli ogrenme": ("reinforcement learning", []),
    "veri bilimi": ("data science", []),
    "siber güvenlik": ("cybersecurity", ["cyber security"]),
    "siber guvenlik": ("cybersecurity", ["cyber security"]),
    "kuantum hesaplama": ("quantum computing", []),
    "iklim değişikliği": ("climate change", []),
    "iklim degisikligi": ("climate change", []),
    "kalp yetmezliği": ("heart failure", ["cardiac failure"]),
    "kalp yetmezligi": ("heart failure", ["cardiac failure"]),
    "kanser": ("cancer", ["neoplasm"]),
    "diyabet": ("diabetes", ["diabetes mellitus"]),
    "bağışıklık": ("immunity", ["immune system"]),
    "bagisiklik": ("immunity", ["immune system"]),
    "genetik": ("genetics", []),
    "biyoinformatik": ("bioinformatics", []),
    "nörobilim": ("neuroscience", []),
    "norobilim": ("neuroscience", []),
    "halk sağlığı": ("public health", []),
    "halk sagligi": ("public health", []),
    "malzeme bilimi": ("materials science", []),
    "yenilenebilir enerji": ("renewable energy", []),
    "robotik": ("robotics", []),
}

MAX_TOKENS_KEYWORD_TRANSLATE = 700

#: Cap on how many unresolved terms go into one batch call. A user with 60
#: interests should not produce one enormous prompt; the remainder is picked
#: up by the next scan (translations persist, so it converges).
KEYWORD_TRANSLATE_MAX_ITEMS = 25

_KEYWORD_TRANSLATE_SYSTEM_TR = """Sen akademik literatür taraması için sorgu \
terimi hazırlayan bir asistansın. Sana bir kullanıcının ilgi alanı anahtar \
kelimeleri verilecek (çoğu Türkçe, bazıları İngilizce olabilir). Görevin her \
terim için akademik veritabanlarında (arXiv, PubMed, Semantic Scholar) \
aranacak İngilizce karşılığını üretmek.

Cevabını şu JSON şemasına uygun döndür:

{
  "translations": [
    {"term": "verilen terimin AYNISI", "en": "kanonik İngilizce karşılık", "variants": ["yaygın eşanlamlı 1", "..."]}
  ]
}

Kurallar:
- translations listesi verilen HER terim için tam olarak bir madde içermeli.
- "term" alanı sana verilen terimin birebir kopyası olmalı — değiştirme.
- Terim zaten İngilizce ise "en" alanına terimi aynen yaz.
- "en" alanı literatürde fiilen kullanılan terim olmalı (birebir sözlük \
çevirisi değil): "kalp yetmezliği" → "heart failure".
- "variants" en fazla 2 madde: yaygın eşanlamlı veya standart kısaltma. \
Yoksa boş liste ver.
- Sadece JSON döndür, başına/sonuna metin ekleme."""


def _lexicon_translation(term: str) -> tuple[str, list[str]] | None:
    """(english, variants) from `_KEYWORD_EN_LEXICON`, or None."""
    return _KEYWORD_EN_LEXICON.get(term.strip().lower())


def translate_keywords(keywords: list[str], *, user=None) -> dict[str, dict]:
    """Map research-interest keywords to their English search form.

    Returns `{original_term: {"en": str, "variants": list[str]}}` — only for
    terms that were actually resolved. A term missing from the result means
    "we could not translate this", and the caller must leave it untranslated
    rather than storing a guess (see `service.ensure_keyword_translations`).

    Lexicon fast-path first; whatever is left goes into ONE batch `_call_llm`
    call with a dict-guard and a single repair retry — same shape as
    `classify_user_topics`/`generate_digest`. No LLM configured means only the
    lexicon answers, which is a valid degraded mode: the untranslated terms
    are simply searched as typed, exactly as before this feature existed.

    Caching lives one level up, on the `Keyword` row, because that table is
    global — the second user to follow "kalp yetmezliği" costs nothing.
    """
    terms = []
    seen: set[str] = set()
    for raw in keywords:
        cleaned = (raw or "").strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            terms.append(cleaned)
    if not terms:
        return {}

    out: dict[str, dict] = {}
    unresolved: list[str] = []
    for term in terms:
        hit = _lexicon_translation(term)
        if hit:
            out[term] = {"en": hit[0], "variants": list(hit[1])}
        else:
            unresolved.append(term)

    if not unresolved or not is_ai_enabled(user):
        return out

    batch = unresolved[:KEYWORD_TRANSLATE_MAX_ITEMS]
    user_msg = "Terimler:\n" + "\n".join(f"- {t}" for t in batch)
    parsed, _raw = _call_llm(
        system=_KEYWORD_TRANSLATE_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=MAX_TOKENS_KEYWORD_TRANSLATE,
        user=user,
    )
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
            "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
            "yalnızca şemadaki nesneyi ver."
        )
        parsed, _raw = _call_llm(
            system=_KEYWORD_TRANSLATE_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=MAX_TOKENS_KEYWORD_TRANSLATE,
            user=user,
        )
    if not isinstance(parsed, dict):
        logger.warning("keyword_translate_parsed_not_object", count=len(batch))
        return out

    # Match the model's echoed term back case-insensitively — models routinely
    # return "Kalp Yetmezliği" for "kalp yetmezliği", and dropping those would
    # mean paying for a call and storing nothing.
    by_lower = {t.lower(): t for t in batch}
    rows = parsed.get("translations")
    if not isinstance(rows, list):
        logger.warning("keyword_translate_missing_list", count=len(batch))
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        original = by_lower.get(str(row.get("term", "")).strip().lower())
        english = _safe_str(row.get("en"))
        if not original or not english:
            continue
        # String(128) on the column — a model that returns a sentence instead
        # of a term would otherwise blow up the insert.
        if len(english) > 128:
            logger.warning("keyword_translate_too_long", term=original)
            continue
        variants = [v for v in (_safe_list(row.get("variants")) or []) if len(v) <= 128][:2]
        out[original] = {"en": english, "variants": variants}

    logger.info(
        "keywords_translated",
        requested=len(terms),
        from_lexicon=len(terms) - len(unresolved),
        from_llm=len(out) - (len(terms) - len(unresolved)),
    )
    return out


def _safe_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    return str(v)


def _safe_list(v: Any) -> list[str] | None:
    if not v:
        return None
    if isinstance(v, list):
        out = [str(item).strip() for item in v if str(item).strip()]
        return out or None
    return [str(v).strip()]
