"""Claude API integration — paper analysis & translation cache.

Two public flows:

  * get_or_generate_analysis(paper, target_lang="tr", *, force=False)
      → returns a PaperAnalysis (or None if AI is disabled).
        On cache hit returns the stored row; on miss (or force) calls
        Claude, persists the row, returns it.

  * get_or_generate_translation(paper, target_lang="tr", *, force=False)
      → same shape, for the Title+Abstract translation.

Both helpers are *idempotent under cache* — repeat calls do not ping
Claude. The route layer is responsible for the user-facing "trigger" UI;
this module just owns the model + cache + Claude wire-up.

`is_ai_enabled()` reports whether `ANTHROPIC_API_KEY` is set in config —
templates can branch on it to show a "configure to enable" hint instead
of a crash if a researcher hits the AI tab without a key.

Failure handling: on any Claude error (no key, timeout, parse failure) we
log + return None and let the route render a "couldn't generate" panel.
The cache is never poisoned with a partial / error response.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from flask import current_app

from app.extensions import db
from app.modules.scrape.models import Paper, PaperAnalysis, PaperTranslation

logger = structlog.get_logger()

# Use a recent Claude model. Override via ANTHROPIC_MODEL env var in
# production if you want to test 4.8 / Opus / Haiku tiers.
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS_ANALYSIS = 1200
MAX_TOKENS_TRANSLATION = 1500


# ----------------------------------------------------------------------------
# Capability gate
# ----------------------------------------------------------------------------


def is_ai_enabled() -> bool:
    """True iff ANTHROPIC_API_KEY is configured. Cheap call — used by
    templates on every AI-tab render to pick the right empty state."""
    return bool((current_app.config.get("ANTHROPIC_API_KEY") or "").strip())


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

_TRANSLATION_SYSTEM_TR = """Akademik metin çevirmenisin. Sana verilen \
İngilizce makale başlığı ve özetini akıcı, terimleri koruyan Türkçe'ye \
çevir. Çıktıyı şu JSON şemasında döndür:

{
  "title_translated": "...",
  "abstract_translated": "..."
}

Sadece JSON döndür."""


# ----------------------------------------------------------------------------
# Claude call helpers
# ----------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """Some models occasionally wrap JSON in ``` fences. Be tolerant."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


def _call_claude(*, system: str, user_msg: str, max_tokens: int) -> tuple[dict | None, str | None]:
    """Single Claude call returning (parsed_json, raw_text). Either may be
    None on failure — caller decides what to do."""
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


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------


def get_analysis(paper: Paper, *, target_lang: str = "tr") -> PaperAnalysis | None:
    """Cache-only lookup. Returns None if no analysis exists yet."""
    return PaperAnalysis.query.filter_by(paper_id=paper.id, target_lang=target_lang).first()


def generate_analysis(paper: Paper, *, target_lang: str = "tr") -> PaperAnalysis | None:
    """Force a Claude call and upsert the cache row. Returns None on failure."""
    if not is_ai_enabled():
        return None
    if target_lang != "tr":
        # Only TR analysis is wired up for now. Other locales fall back to
        # the dev's preferred language when the user explicitly asks.
        target_lang = "tr"

    title = (paper.title or "").strip()
    abstract = (paper.abstract or "").strip()
    user_msg = f"Makale başlığı:\n{title}\n\nÖzet:\n{abstract or '(özet yok)'}"

    parsed, raw = _call_claude(
        system=_ANALYSIS_SYSTEM_TR, user_msg=user_msg, max_tokens=MAX_TOKENS_ANALYSIS
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
        model_version=_model(),
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
    logger.info("analysis_generated", paper_id=paper.id, model=_model())
    return analysis


def get_or_generate_analysis(
    paper: Paper, *, target_lang: str = "tr", force: bool = False
) -> PaperAnalysis | None:
    """Returns a cached analysis or generates one. None means AI is off or
    the call failed — caller renders the disabled / retry UI."""
    if not force:
        cached = get_analysis(paper, target_lang=target_lang)
        if cached is not None:
            return cached
    return generate_analysis(paper, target_lang=target_lang)


# ----------------------------------------------------------------------------
# Translation
# ----------------------------------------------------------------------------


def get_translation(paper: Paper, *, target_lang: str = "tr") -> PaperTranslation | None:
    return PaperTranslation.query.filter_by(paper_id=paper.id, target_lang=target_lang).first()


def generate_translation(paper: Paper, *, target_lang: str = "tr") -> PaperTranslation | None:
    if not is_ai_enabled():
        return None
    title = (paper.title or "").strip()
    abstract = (paper.abstract or "").strip()
    if not title and not abstract:
        return None
    user_msg = f"Başlık:\n{title}\n\nÖzet:\n{abstract or '(özet yok)'}"

    parsed, _raw = _call_claude(
        system=_TRANSLATION_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=MAX_TOKENS_TRANSLATION,
    )
    if parsed is None:
        return None

    existing = get_translation(paper, target_lang=target_lang)
    fields = dict(
        title_translated=_safe_str(parsed.get("title_translated")),
        abstract_translated=_safe_str(parsed.get("abstract_translated")),
        model_version=_model(),
    )
    if existing is None:
        tr = PaperTranslation(paper_id=paper.id, target_lang=target_lang, **fields)
        db.session.add(tr)
    else:
        for k, v in fields.items():
            setattr(existing, k, v)
        tr = existing
    db.session.commit()
    logger.info("translation_generated", paper_id=paper.id, model=_model())
    return tr


def get_or_generate_translation(
    paper: Paper, *, target_lang: str = "tr", force: bool = False
) -> PaperTranslation | None:
    if not force:
        cached = get_translation(paper, target_lang=target_lang)
        if cached is not None:
            return cached
    return generate_translation(paper, target_lang=target_lang)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


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
