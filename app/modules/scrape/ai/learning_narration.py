"""Narration of the learning-map graph."""

from __future__ import annotations

import json

import structlog

from app.modules.scrape import ai_service

logger = structlog.get_logger()


# ----------------------------------------------------------------------------
# Learning map narration (Faz 9.3)
# ----------------------------------------------------------------------------

#: One call for the whole map: up to 12 nodes x (label + 2 sentences + 1
#: question). Turkish tokenises heavier than English, hence the headroom.
MAX_TOKENS_LEARNING_MAP = 2500


#: Output caps. The schema asks for these limits; these enforce them, because
#: a small model that writes a paragraph would otherwise break the step card.
LEARNING_MAP_LABEL_CHARS = 40


LEARNING_MAP_EXPLAIN_CHARS = 360


LEARNING_MAP_QUESTION_CHARS = 200


#: Output language by active locale. A locale we do not ship falls back to
#: Turkish, the app default -- same as `select_locale`.
_LEARNING_MAP_LANGUAGES = {"tr": "Türkçe", "en": "İngilizce (English)"}


_LEARNING_MAP_SYSTEM_TR = """Sen bir araştırma-öğrenme asistanısın. Bir \
kullanıcı yeni bir konuya giriyor ve "nereden başlamalıyım?" diye soruyor. Sana \
o konunun LLM kullanılmadan hesaplanmış bir kavram haritası verilecek: \
kavramlar (düğümler), her birinin külliyattaki çalışma sayısı ve rolü, \
kavramlar arasındaki oklar ve her kavramın anahtar çalışmasının başlığı.

Oklar sabittir: "A → B", B'nin A'nın üzerine kurulduğu anlamına gelir. Ok \
ekleyemez, silemez, yönünü değiştiremezsin; sıra ve yapı senin işin değil. \
Görevin yalnızca her düğümü okuyucuya tanıtmak.

Roller: prerequisite = ön koşul, core = çekirdek, method = yöntem, \
current = güncel.

Kurallar:
- Yalnızca verilen girdiye dayan; uydurma çalışma, yazar ya da sonuç ekleme.
- "label": kavramın sade, kısa adı (en fazla 40 karakter).
- "explain": en fazla 2 cümle — bu kavram ne ve haritada neden bu konumda.
- "check_question": okuyucunun "bunu anladım mı" diye durmasını sağlayan tek \
bir soru. Cevabını yazma.
- Sana verilen HER düğüm için tam olarak bir madde döndür; "id" alanını sana \
verildiği haliyle birebir kopyala, yeni düğüm ekleme, düğüm atlama.

Cevabını DAİMA {language} ver ve şu JSON şemasına uygun döndür:

{{
  "nodes": [
    {{"id": "düğüm id'si, birebir", "label": "...", "explain": "...", "check_question": "..."}}
  ]
}}

Sadece JSON döndür, başına/sonuna metin ekleme."""


def _learning_map_language() -> str:
    """The active locale's language name for the prompt.

    Read from Flask-Babel rather than taken as a parameter so the caller's
    `force_locale(<requester's locale>)` decides it -- the same contract every
    other background `_()` already follows (CLAUDE.md, report_tasks).
    """
    from flask_babel import get_locale

    try:
        locale = get_locale()
    except Exception:  # pragma: no cover - no app context; never raise here
        locale = None
    lang = getattr(locale, "language", None) or "tr"
    return ai_service._LEARNING_MAP_LANGUAGES.get(lang, ai_service._LEARNING_MAP_LANGUAGES["tr"])


def _clip(text: str | None, n: int) -> str:
    """`_truncate`, but at a word boundary when one is reasonably close."""
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    space = cut.rfind(" ")
    if space >= n // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def narrate_learning_map(graph: dict, *, topic: str, user=None) -> dict | None:
    """Name, explain and pose a check question for each node of a learning map.

    `graph` is `learning_map.learning_map_graph`'s output. Only the node
    fields `id`, `name`, `role`, `count`, `layer` and `key_work.title` (plus
    `key_work.abstract`, truncated, if a future payload carries one -- today's
    does not) go into the prompt; of the edges only `from`/`to` are read, so
    the edge weight can change (subsumption -> citation flow) without touching
    this function. No full text is ever sent (SCRAPING.md §11): the card's job
    is orientation, not a summary.

    Single call, no map/reduce: the input is a computed graph of at most
    `max_nodes` concepts, not a corpus.

    Returns a dict keyed by node id -- the caller looks a card up by the id
    it already holds, so a list would only make it search::

        {"nodes": {node_id: {"label": str, "explain": str,
                             "check_question": str}}}

    `label` is clipped to `LEARNING_MAP_LABEL_CHARS`, `explain` and
    `check_question` likewise; a missing or empty field becomes "" (the page
    falls back to the OpenAlex name / an empty card) rather than failing the
    whole narration.

    Returns `None` -- never raises -- when AI is disabled, the graph is
    insufficient or has no nodes (no call is made), the call/parse fails after
    one repair retry, the answer is not `{"nodes": [object, ...]}`, or the set
    of returned ids is not exactly the set sent (a missing, invented or
    duplicated node). The model cannot touch the edges: the schema has no
    field for them. A `None` here means the caller keeps the map and marks
    the report `partial` (docs/PHASE9.md §2).

    The output language is the active locale's; the caller wraps the call in
    `force_locale(<requester's locale>)`.
    """
    if not isinstance(graph, dict) or graph.get("insufficient"):
        return None
    sent: dict[str, dict] = {}
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        node_id = ai_service._safe_str(n.get("id"))
        if node_id and node_id not in sent:
            sent[node_id] = n
    if not sent:
        return None
    if not ai_service.is_ai_enabled(user):
        return None

    node_lines = []
    for node_id, n in sent.items():
        raw_key_work = n.get("key_work")
        key_work: dict = raw_key_work if isinstance(raw_key_work, dict) else {}
        line = {
            "id": node_id,
            "name": ai_service._safe_str(n.get("name")) or node_id,
            "role": n.get("role"),
            "count": n.get("count"),
            "layer": n.get("layer"),
            "key_work_title": ai_service._safe_str(key_work.get("title")),
        }
        abstract = ai_service._truncate(key_work.get("abstract"), ai_service.REPORT_ABSTRACT_CHARS)
        if abstract:
            line["key_work_abstract"] = abstract
        node_lines.append(json.dumps(line, ensure_ascii=False))

    arrows = []
    for e in graph.get("edges") or []:
        if not isinstance(e, dict):
            continue
        src, dst = ai_service._safe_str(e.get("from")), ai_service._safe_str(e.get("to"))
        if src in sent and dst in sent:
            arrows.append(f"{src} → {dst}")

    system = ai_service._LEARNING_MAP_SYSTEM_TR.format(language=ai_service._learning_map_language())
    user_msg = (
        f"Konu: {ai_service._clip(topic, 200)}\n\n"
        "Düğümler:\n" + "\n".join(node_lines) + "\n\n"
        "Oklar (A → B: B, A'nın üzerine kurulu):\n" + ("\n".join(arrows) or "(ok yok)")
    )

    parsed, _raw = ai_service._call_llm(
        system=system, user_msg=user_msg, max_tokens=ai_service.MAX_TOKENS_LEARNING_MAP, user=user
    )
    if not isinstance(parsed, dict):
        retry_msg = (
            user_msg + "\n\nÖNEMLİ: Yanıtın DOĞRUDAN geçerli bir JSON NESNESİ olmalı — '{' ile "
            "başlayıp '}' ile bitmeli. Metin, açıklama, string veya liste döndürme; "
            "yalnızca şemadaki nesneyi ver."
        )
        parsed, _raw = ai_service._call_llm(
            system=system,
            user_msg=retry_msg,
            max_tokens=ai_service.MAX_TOKENS_LEARNING_MAP,
            user=user,
        )
    if not isinstance(parsed, dict):
        logger.warning("learning_map_parsed_not_object", user_id=getattr(user, "id", None))
        return None

    rows = parsed.get("nodes")
    if not isinstance(rows, list):
        logger.warning("learning_map_nodes_not_list", user_id=getattr(user, "id", None))
        return None

    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            logger.warning("learning_map_node_invalid", user_id=getattr(user, "id", None))
            return None
        node_id = ai_service._safe_str(row.get("id"))
        if node_id is None or node_id in out:
            logger.warning("learning_map_node_invalid", user_id=getattr(user, "id", None))
            return None
        out[node_id] = {
            "label": ai_service._clip(
                ai_service._safe_str(row.get("label")), ai_service.LEARNING_MAP_LABEL_CHARS
            ),
            "explain": ai_service._clip(
                ai_service._safe_str(row.get("explain")), ai_service.LEARNING_MAP_EXPLAIN_CHARS
            ),
            "check_question": ai_service._clip(
                ai_service._safe_str(row.get("check_question")),
                ai_service.LEARNING_MAP_QUESTION_CHARS,
            ),
        }

    if set(out) != set(sent):
        # A missing node leaves a card unexplained; an invented one is a
        # concept the numbers never produced. Either way the narration no
        # longer describes the map it sits on.
        logger.warning(
            "learning_map_id_mismatch",
            user_id=getattr(user, "id", None),
            missing=len(set(sent) - set(out)),
            extra=len(set(out) - set(sent)),
        )
        return None

    logger.info(
        "learning_map_narrated",
        user_id=getattr(user, "id", None),
        nodes=len(out),
        edges=len(arrows),
    )
    return {"nodes": out}
