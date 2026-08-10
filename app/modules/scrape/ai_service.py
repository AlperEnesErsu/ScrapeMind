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
cache is never poisoned with a partial / error response.
"""

from __future__ import annotations

import json
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
    way the package is only required when AI is actually exercised."""
    from anthropic import Anthropic

    return Anthropic(api_key=current_app.config["ANTHROPIC_API_KEY"])


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


def _call_claude(
    *, system: str, user_msg: str, max_tokens: int, expect_json: bool = True
) -> tuple[Any | None, str | None]:
    """Single Claude call returning (parsed_json_or_text, raw_text). Either
    may be None on failure — caller decides what to do. `expect_json=False`
    skips JSON parsing and returns the stripped raw text as the first
    element (used by the RAG chat, which wants prose, not JSON)."""
    try:
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
    except Exception:
        # Network error, missing key, model not available — log + give up.
        logger.exception("claude_call_failed")
        return None, None


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
    (OpenRouter or Ollama). Same (parsed, raw) contract as `_call_claude`."""
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
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
        except Exception:
            # Some backends (older Ollama builds) reject response_format —
            # retry once without it before giving up.
            if expect_json and "response_format" in kwargs:
                kwargs.pop("response_format")
                resp = client.chat.completions.create(**kwargs)
            else:
                raise

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
    except Exception:
        logger.exception("openai_compatible_call_failed", model=model)
        return None, None


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
