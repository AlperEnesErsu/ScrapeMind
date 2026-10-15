"""Patent novelty assessment and claim explanation."""

from __future__ import annotations

import structlog

from app.modules.scrape import ai_service

logger = structlog.get_logger()


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
    for p in patents[: ai_service.NOVELTY_MAX_ITEMS]:
        assignees = [
            c[len("assignee:") :]
            for c in (getattr(p, "categories", None) or [])
            if isinstance(c, str) and c.startswith("assignee:")
        ]
        year = getattr(getattr(p, "published_at", None), "year", None)
        owner = f" · {assignees[0]}" if assignees else ""
        lines.append(
            f"- [{p.external_id}] {p.title} ({year or 'tarih yok'}){owner}\n"
            f"  {ai_service._truncate(p.abstract, ai_service.NOVELTY_ABSTRACT_CHARS) or 'Özet yok.'}"
        )

    user_msg = "Buluş fikri:\n" + idea + "\n\nİlgili patentler:\n" + "\n".join(lines)

    parsed, _raw = ai_service._call_llm(
        system=ai_service._NOVELTY_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_NOVELTY,
        user=user,
    )
    if not isinstance(parsed, dict):
        return None
    return parsed


# ----------------------------------------------------------------------------
# Patent claim explanation (Faz 8.6)
# ----------------------------------------------------------------------------

MAX_TOKENS_CLAIM_EXPLAIN = 700


#: Per claim in the prompt. A claim longer than this is rare; truncating it is
#: better than letting one pathological claim decide the provider bill.
CLAIM_EXPLAIN_CHARS = 2500


CLAIM_EXPLAIN_MAX_TERMS = 6


_CLAIM_EXPLAIN_SYSTEM_TR = (
    "Sen bir patent okuma asistanısın. Sana bir patent isteminin metnini ve, "
    "istem başka bir isteme bağlıysa, bağlı olduğu istemlerin metinlerini "
    "vereceğim. Görevin, hedef istemi patent hukuku bilmeyen bir araştırmacının "
    "anlayacağı düz Türkçeyle açıklamak.\n\n"
    "Kurallar:\n"
    "- Yalnızca sana verilen istem metinlerine dayan. Metinde olmayan bir "
    "özelliği, amacı, avantajı ya da uygulama alanını ekleme.\n"
    "- İstemin kapsamını genişletme ya da daraltma; ne diyorsa onu söyle.\n"
    "- Metin belirsizse bunu açıkça söyle, tahminle doldurma.\n"
    "- Hukuki tavsiye verme, ihlal ya da geçerlilik yorumu yapma.\n\n"
    'Yanıtı şu JSON şemasıyla ver: {"plain": "...", "narrows": "...", '
    '"terms": [{"term": "...", "meaning": "..."}]}\n'
    '"plain": hedef istemin 2-4 cümlelik düz Türkçe açıklaması. '
    '"narrows": hedef istem başka bir isteme bağlıysa, o isteme neyi eklediği '
    "ya da neyi daralttığı; bağımsızsa boş metin. "
    '"terms": okuyucunun bilmesi gereken en fazla 6 teknik ya da patent '
    "terimi ve metne dayalı kısa anlamları; gerek yoksa boş liste — uydurma."
)


def explain_patent_claim(target, ancestors: list, *, user=None) -> dict | None:
    """Plain-language reading of one claim, grounded in the claim text alone.

    `target` and `ancestors` are claim-like objects (`number`, `text`);
    `ancestors` runs from the independent claim down to the direct parent. A
    dependent claim ("The method of claim 3, wherein ...") means nothing
    without the chain above it, so the chain goes into the prompt.

    Returns a normalised dict or None -- None when AI is unavailable or the
    model returned nothing usable. The page keeps showing the original claim
    either way; the reading is an aid next to it, never a replacement.
    """
    text = (getattr(target, "text", None) or "").strip()
    if not text:
        return None

    parts = []
    for claim in ancestors:
        parts.append(
            f"İstem {claim.number} (bağlam):\n{ai_service._truncate(claim.text, ai_service.CLAIM_EXPLAIN_CHARS)}"
        )
    parts.append(
        f"İstem {target.number} (AÇIKLANACAK HEDEF):\n{ai_service._truncate(text, ai_service.CLAIM_EXPLAIN_CHARS)}"
    )

    parsed, _raw = ai_service._call_llm(
        system=ai_service._CLAIM_EXPLAIN_SYSTEM_TR,
        user_msg="\n\n".join(parts),
        max_tokens=ai_service.MAX_TOKENS_CLAIM_EXPLAIN,
        user=user,
    )
    if not isinstance(parsed, dict):
        return None
    plain = ai_service._safe_str(parsed.get("plain"))
    if not plain:
        return None

    terms = []
    for item in parsed.get("terms") or []:
        if not isinstance(item, dict):
            continue
        term, meaning = (
            ai_service._safe_str(item.get("term")),
            ai_service._safe_str(item.get("meaning")),
        )
        if term and meaning:
            terms.append({"term": term, "meaning": meaning})
        if len(terms) >= ai_service.CLAIM_EXPLAIN_MAX_TERMS:
            break

    return {
        "plain": plain,
        # An independent claim narrows nothing; a model that says otherwise
        # is describing a relationship the text does not have.
        "narrows": ai_service._safe_str(parsed.get("narrows")) if ancestors else None,
        "terms": terms,
    }
