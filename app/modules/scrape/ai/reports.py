"""Report and digest generation (map/reduce summaries, author-group reports, digest)."""

from __future__ import annotations

import json

import structlog

from app.extensions import db
from app.modules.scrape import ai_service
from app.modules.scrape.models import (
    UserDigest,
)

logger = structlog.get_logger()


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
    if not ai_service.is_ai_enabled(user):
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
        abstract = ai_service._truncate(item.get("abstract"), ai_service.REPORT_ABSTRACT_CHARS)
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

    parsed, _raw = ai_service._call_llm(
        system=ai_service._REPORT_MAP_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_REPORT_MAP,
        user=user,
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
        parsed, _raw = ai_service._call_llm(
            system=ai_service._REPORT_MAP_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=ai_service.MAX_TOKENS_REPORT_MAP,
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
            why = ai_service._safe_str(n.get("why"))
            if why is None:
                continue
            notable.append({"ref": ref_idx, "why": why})

    result = {
        "themes": ai_service._safe_list(parsed.get("themes")) or [],
        "notable": notable,
        "methods": ai_service._safe_list(parsed.get("methods")) or [],
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
    if not ai_service.is_ai_enabled(user):
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

    parsed, _raw = ai_service._call_llm(
        system=ai_service._REPORT_REDUCE_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_REPORT_REDUCE,
        user=user,
    )
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
            "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
            "yalnızca şemadaki nesneyi ver."
        )
        parsed, _raw = ai_service._call_llm(
            system=ai_service._REPORT_REDUCE_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=ai_service.MAX_TOKENS_REPORT_REDUCE,
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
                    "themes": ai_service._safe_list(t.get("themes")) or [],
                    "notable": ai_service._safe_list(t.get("notable")) or [],
                }
            )

    key_works_raw = parsed.get("key_works") or []
    key_works: list[dict] = []
    if isinstance(key_works_raw, list):
        for kw in key_works_raw:
            if not isinstance(kw, dict):
                continue
            title = ai_service._safe_str(kw.get("title"))
            why = ai_service._safe_str(kw.get("why"))
            if not title or not why:
                continue
            key_works.append({"title": title, "why": why})

    result = {
        "tldr": ai_service._safe_str(parsed.get("tldr")) or "",
        "timeline": timeline,
        "emerging": ai_service._safe_list(parsed.get("emerging")) or [],
        "fading": ai_service._safe_list(parsed.get("fading")) or [],
        "key_works": key_works,
        "key_authors": ai_service._safe_list(parsed.get("key_authors")) or [],
        "key_venues": ai_service._safe_list(parsed.get("key_venues")) or [],
        "for_your_keywords": ai_service._safe_str(parsed.get("for_your_keywords")) or "",
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
    if not ai_service.is_ai_enabled(user):
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

    parsed, _raw = ai_service._call_llm(
        system=ai_service._AUTHOR_GROUP_REDUCE_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_REPORT_REDUCE,
        user=user,
    )
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nONEMLI: Yanitin DOGRUDAN gecerli bir JSON NESNESI olmali - '{' ile "
            "baslayip '}' ile bitmeli. Metin, aciklama, string veya liste dondurme; "
            "yalnizca semadaki nesneyi ver."
        )
        parsed, _raw = ai_service._call_llm(
            system=ai_service._AUTHOR_GROUP_REDUCE_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=ai_service.MAX_TOKENS_REPORT_REDUCE,
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
        name = ai_service._safe_str(m.get("name"))
        if not name or (known_names and name not in known_names):
            continue
        members.append(
            {
                "name": name,
                "focus": ai_service._safe_str(m.get("focus")) or "",
                "recent_highlights": ai_service._safe_list(m.get("recent_highlights")) or [],
            }
        )

    result = {
        "tldr": ai_service._safe_str(parsed.get("tldr")) or "",
        "members": members,
        "shared_topics": ai_service._safe_list(parsed.get("shared_topics")) or [],
        "overlap_with_you": ai_service._safe_str(parsed.get("overlap_with_you")) or "",
        "coauthorship_notes": ai_service._safe_str(parsed.get("coauthorship_notes")) or "",
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
    if not ai_service.is_ai_enabled(user):
        return None
    if not links:
        return None

    window = links[: ai_service.DIGEST_MAX_ITEMS]
    lines = []
    for idx, link in enumerate(window, start=1):
        paper = link.paper
        title = (paper.title or "").strip()
        abstract = ai_service._truncate(paper.abstract, 500)
        kw = link.matched_keyword or ""
        lines.append(
            f"{idx}. Başlık: {title}\n   İlgili anahtar kelime: {kw}\n   Özet: {abstract or '(özet yok)'}"
        )
    user_msg = "Aşağıdaki makaleleri özetle:\n\n" + "\n\n".join(lines)

    parsed, raw = ai_service._call_llm(
        system=ai_service._DIGEST_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_DIGEST,
        user=user,
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
        parsed, raw = ai_service._call_llm(
            system=ai_service._DIGEST_SYSTEM_TR,
            user_msg=retry_msg,
            max_tokens=ai_service.MAX_TOKENS_DIGEST,
            user=user,
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
                "title": ai_service._safe_str(h.get("title")) or "",
                "why": ai_service._safe_str(h.get("why")) or "",
                "user_paper_id": user_paper_id,
            }
            highlights.append(entry)

    fields = dict(
        period_end=period_end,
        summary=ai_service._safe_str(parsed.get("summary")),
        highlights=highlights or None,
        themes=ai_service._safe_list(parsed.get("themes")),
        item_count=len(window),
        model_version=ai_service._model_label(user),
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
        model=ai_service._model_label(user),
    )
    return digest
