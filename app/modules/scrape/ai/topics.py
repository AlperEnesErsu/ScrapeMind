"""Topic classification, keyword translation and feed relevance scoring."""

from __future__ import annotations

from typing import Any

import structlog

from app.extensions import db
from app.modules.scrape import ai_service

logger = structlog.get_logger()


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
    if not ai_service.is_ai_enabled(user):
        return []
    if not papers:
        return []

    from app.modules.academic.service import list_user_keywords

    keywords = [kw.value for kw in list_user_keywords(user)]
    if not keywords:
        return []

    window = papers[: ai_service.FEED_SCORE_MAX_ITEMS]
    lines = []
    for idx, paper in enumerate(window, start=1):
        title = (paper.title or "").strip()
        abstract = ai_service._truncate(paper.abstract, 400)
        lines.append(f"{idx}. Başlık: {title}\n   Özet: {abstract or '(özet yok)'}")
    user_msg = (
        "Kullanıcının ilgi alanı anahtar kelimeleri: "
        + ", ".join(keywords)
        + "\n\nDuyurular:\n\n"
        + "\n\n".join(lines)
    )

    parsed, _raw = ai_service._call_llm(
        system=ai_service._FEED_SCORE_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_FEED_SCORE,
        user=user,
    )
    # Small/weak models sometimes emit a JSON scalar or bare list instead of
    # the object schema — same one-shot repair retry as generate_digest.
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
            "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
            "yalnızca şemadaki nesneyi ver."
        )
        parsed, _raw = ai_service._call_llm(
            system=ai_service._FEED_SCORE_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=ai_service.MAX_TOKENS_FEED_SCORE,
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
                "why": ai_service._safe_str(item.get("why")) or "",
                "matched_keyword": ai_service._safe_str(item.get("matched_keyword")),
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
    settings_row = ai_service._user_settings_row(user)
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

    settings_row = ai_service._user_settings_row(user)
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
    for topic, terms in ai_service._TOPIC_LEXICON.items():
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

    kw_hash = ai_service._topics_hash(keywords)
    cached_keys, cached_hash = ai_service._cached_user_topics(user)
    if cached_keys is not None and cached_hash == kw_hash:
        return cached_keys

    topics: set[str] = set()
    unresolved: list[str] = []
    for kw in keywords:
        matched = ai_service._lexicon_topics(kw)
        if matched:
            topics |= matched
        else:
            unresolved.append(kw)

    if unresolved:
        user_msg = "Anahtar kelimeler: " + ", ".join(unresolved)
        parsed, _raw = ai_service._call_llm(
            system=ai_service._TOPIC_CLASSIFY_SYSTEM_TR,
            user_msg=user_msg,
            max_tokens=ai_service.MAX_TOKENS_TOPIC_CLASSIFY,
            user=user,
        )
        if not isinstance(parsed, dict):
            retry_msg = (
                user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
                "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
                "yalnızca şemadaki nesneyi ver."
            )
            parsed, _raw = ai_service._call_llm(
                system=ai_service._TOPIC_CLASSIFY_SYSTEM_TR,
                user_msg=retry_msg,
                max_tokens=ai_service.MAX_TOKENS_TOPIC_CLASSIFY,
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
    ai_service._store_user_topics(user, result, kw_hash)
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
    return ai_service._KEYWORD_EN_LEXICON.get(term.strip().lower())


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
        hit = ai_service._lexicon_translation(term)
        if hit:
            out[term] = {"en": hit[0], "variants": list(hit[1])}
        else:
            unresolved.append(term)

    if not unresolved or not ai_service.is_ai_enabled(user):
        return out

    batch = unresolved[: ai_service.KEYWORD_TRANSLATE_MAX_ITEMS]
    user_msg = "Terimler:\n" + "\n".join(f"- {t}" for t in batch)
    parsed, _raw = ai_service._call_llm(
        system=ai_service._KEYWORD_TRANSLATE_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_KEYWORD_TRANSLATE,
        user=user,
    )
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
            "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
            "yalnızca şemadaki nesneyi ver."
        )
        parsed, _raw = ai_service._call_llm(
            system=ai_service._KEYWORD_TRANSLATE_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=ai_service.MAX_TOKENS_KEYWORD_TRANSLATE,
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
        english = ai_service._safe_str(row.get("en"))
        if not original or not english:
            continue
        # String(128) on the column — a model that returns a sentence instead
        # of a term would otherwise blow up the insert.
        if len(english) > 128:
            logger.warning("keyword_translate_too_long", term=original)
            continue
        variants = [v for v in (ai_service._safe_list(row.get("variants")) or []) if len(v) <= 128][
            :2
        ]
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
