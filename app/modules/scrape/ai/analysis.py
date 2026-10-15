"""Per-paper LLM features: analysis, translation, video summary, Q&A, web summary."""

from __future__ import annotations

import structlog

from app.extensions import db
from app.modules.scrape import ai_service
from app.modules.scrape.models import (
    Paper,
    PaperAnalysis,
    PaperTranslation,
    VideoSummary,
)

logger = structlog.get_logger()


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


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------


def get_analysis(paper: Paper, *, target_lang: str = "tr") -> PaperAnalysis | None:
    """Cache-only lookup. Returns None if no analysis exists yet."""
    return PaperAnalysis.query.filter_by(paper_id=paper.id, target_lang=target_lang).first()


#: How much stored full text reaches the analysis prompt.
#:
#: Sized against the smallest context this app supports rather than the
#: largest -- OpenRouter's free tier is the default provider, and a prompt
#: built for a 200k-token model would simply fail there. The opening of a
#: paper carries the abstract, introduction and method, which is what the
#: analysis actually asks about; discussion and references are the parts a
#: truncation loses, and they are the parts it can afford to lose.
FULLTEXT_ANALYSIS_CHARS = 12_000


def _analysis_user_msg(title: str, abstract: str, paper) -> str:
    """Build the analysis prompt, preferring the paper over its abstract.

    Before full text existed this was title + abstract, and the model was being
    asked for method, findings and limitations from a description written to
    advertise the work. Limitations in particular are rarely in an abstract at
    all -- authors put them in the discussion.

    Only text the licence let us keep is available here; for everything else
    the abstract is still all there is, which is why the abstract stays in the
    prompt rather than being replaced.
    """
    body = (getattr(paper, "fulltext", None) or "").strip()
    parts = [f"Makale başlığı:\n{title}", f"\n\nÖzet:\n{abstract or '(özet yok)'}"]
    if body:
        excerpt = body[: ai_service.FULLTEXT_ANALYSIS_CHARS]
        truncated = len(body) > ai_service.FULLTEXT_ANALYSIS_CHARS
        parts.append(
            "\n\nTam metin" + (" (baştan bir bölüm)" if truncated else "") + f":\n{excerpt}"
        )
    return "".join(parts)


def generate_analysis(paper: Paper, *, target_lang: str = "tr", user=None) -> PaperAnalysis | None:
    """Force an LLM call (routed via `_call_llm` for `user`'s resolved
    provider) and upsert the cache row. Returns None on failure."""
    if not ai_service.is_ai_enabled(user):
        return None
    if target_lang != "tr":
        # Only TR analysis is wired up for now. Other locales fall back to
        # the dev's preferred language when the user explicitly asks.
        target_lang = "tr"

    title = (paper.title or "").strip()
    abstract = (paper.abstract or "").strip()
    user_msg = ai_service._analysis_user_msg(title, abstract, paper)

    parsed, raw = ai_service._call_llm(
        system=ai_service._ANALYSIS_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_ANALYSIS,
        user=user,
    )
    if parsed is None:
        return None

    existing = ai_service.get_analysis(paper, target_lang=target_lang)
    fields = dict(
        tldr=ai_service._safe_str(parsed.get("tldr")),
        method=ai_service._safe_list(parsed.get("method")),
        findings=ai_service._safe_list(parsed.get("findings")),
        limitations=ai_service._safe_list(parsed.get("limitations")),
        personal_relevance=ai_service._safe_str(parsed.get("personal_relevance")),
        model_version=ai_service._model_label(user),
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
    logger.info("analysis_generated", paper_id=paper.id, model=ai_service._model_label(user))
    return analysis


def get_or_generate_analysis(
    paper: Paper, *, target_lang: str = "tr", force: bool = False, user=None
) -> PaperAnalysis | None:
    """Returns a cached analysis or generates one. None means AI is off or
    the call failed — caller renders the disabled / retry UI."""
    if not force:
        cached = ai_service.get_analysis(paper, target_lang=target_lang)
        if cached is not None:
            return cached
    return ai_service.generate_analysis(paper, target_lang=target_lang, user=user)


# ----------------------------------------------------------------------------
# Translation
# ----------------------------------------------------------------------------


def get_translation(paper: Paper, *, target_lang: str = "tr") -> PaperTranslation | None:
    return PaperTranslation.query.filter_by(paper_id=paper.id, target_lang=target_lang).first()


def generate_translation(
    paper: Paper, *, target_lang: str = "tr", user=None
) -> PaperTranslation | None:
    if not ai_service.is_ai_enabled(user):
        return None
    title = (paper.title or "").strip()
    abstract = (paper.abstract or "").strip()
    if not title and not abstract:
        return None
    user_msg = f"Başlık:\n{title}\n\nÖzet:\n{abstract or '(özet yok)'}"

    parsed, _raw = ai_service._call_llm(
        system=ai_service._TRANSLATION_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_TRANSLATION,
        user=user,
    )
    if parsed is None:
        return None

    existing = ai_service.get_translation(paper, target_lang=target_lang)
    fields = dict(
        title_translated=ai_service._safe_str(parsed.get("title_translated")),
        abstract_translated=ai_service._safe_str(parsed.get("abstract_translated")),
        model_version=ai_service._model_label(user),
    )
    if existing is None:
        tr = PaperTranslation(paper_id=paper.id, target_lang=target_lang, **fields)
        db.session.add(tr)
    else:
        for k, v in fields.items():
            setattr(existing, k, v)
        tr = existing
    db.session.commit()
    logger.info("translation_generated", paper_id=paper.id, model=ai_service._model_label(user))
    return tr


def get_or_generate_translation(
    paper: Paper, *, target_lang: str = "tr", force: bool = False, user=None
) -> PaperTranslation | None:
    if not force:
        cached = ai_service.get_translation(paper, target_lang=target_lang)
        if cached is not None:
            return cached
    return ai_service.generate_translation(paper, target_lang=target_lang, user=user)


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
    if not ai_service.is_ai_enabled(user):
        return None

    transcript = (transcript or "").strip()
    if not transcript:
        return None

    title = (paper.title or "").strip()
    user_msg = (
        f"Video başlığı:\n{title}\n\n"
        f"Transkript (otomatik oluşturulmuş, noktalama eksik olabilir):\n"
        f"{ai_service._truncate(transcript, ai_service.VIDEO_TRANSCRIPT_PROMPT_CHARS)}"
    )

    parsed, raw = ai_service._call_llm(
        system=ai_service._VIDEO_SUMMARY_SYSTEM_TR,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_VIDEO_SUMMARY,
        user=user,
    )
    if parsed is None:
        return None

    existing = ai_service.get_video_summary(paper)
    fields = dict(
        tldr=ai_service._safe_str(parsed.get("tldr")),
        highlights=ai_service._safe_list(parsed.get("highlights")),
        topics=ai_service._safe_list(parsed.get("topics")),
        transcript_chars=len(transcript),
        source_lang=source_lang,
        target_lang=target_lang,
        model_version=ai_service._model_label(user),
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
    logger.info("video_summary_generated", paper_id=paper.id, model=ai_service._model_label(user))
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
        cached = ai_service.get_video_summary(paper)
        if cached is not None:
            return cached
    return ai_service.generate_video_summary(paper, transcript, user=user)


def ask_paper(
    paper: Paper, question: str, history: list[dict] | None = None, *, user=None
) -> str | None:
    """Ask a question about a paper using the resolved LLM, with multi-source
    RAG context retrieval (abstract, structured analysis, user notes, and
    semantically retrieved cross-paper library context).

    history is a list of dicts: [{'role': 'user'|'assistant', 'content': '...'}]
    """
    if not ai_service.is_ai_enabled(user):
        return None

    title = (paper.title or "").strip()
    abstract = (paper.abstract or "").strip()

    from app.modules.scrape.models import PaperAnalysis, UserPaper

    # 1. Gather current paper analysis if available
    analysis = PaperAnalysis.query.filter_by(paper_id=paper.id).first()
    analysis_text = ""
    if analysis:
        parts = []
        if analysis.tldr:
            parts.append(f"TL;DR: {analysis.tldr}")
        if analysis.method:
            parts.append("Yöntem: " + "; ".join(analysis.method))
        if analysis.findings:
            parts.append("Temel Bulgular: " + "; ".join(analysis.findings))
        if analysis.limitations:
            parts.append("Kısıtlamalar: " + "; ".join(analysis.limitations))
        if parts:
            analysis_text = "\n\n--- YAPILANDIRILMIŞ MAKALE ANALİZİ ---\n" + "\n".join(parts)

    # 2. Gather user notes if available
    notes_text = ""
    if user is not None:
        user_paper = UserPaper.query.filter_by(user_id=user.id, paper_id=paper.id).first()
        if user_paper and user_paper.notes:
            notes_text = "\n\n--- KULLANICININ BU MAKALE ÜZERİNE NOTLARI ---\n" + "\n".join(
                f"- [{n.tag or 'Not'}]: {n.body}" for n in user_paper.notes
            )

    # 3. True RAG: Semantic retrieval from user's library based on question
    rag_library_text = ""
    if user is not None and question:
        try:
            from app.modules.scrape.embedding_service import (
                current_embedding_model,
                get_embedding,
            )

            q_vector = get_embedding(question, user=user)
            if q_vector is not None:
                dist_col = Paper.embedding.cosine_distance(q_vector).label("dist")
                related_papers = (
                    db.session.query(Paper, dist_col)
                    .join(UserPaper, UserPaper.paper_id == Paper.id)
                    .filter(
                        UserPaper.user_id == user.id,
                        Paper.id != paper.id,
                        UserPaper.dismissed_at.is_(None),
                        Paper.embedding.is_not(None),
                        # Only vectors the question's vector can be compared with.
                        Paper.embedding_model == current_embedding_model(),
                    )
                    .order_by(dist_col.asc())
                    .limit(3)
                    .all()
                )
                close_papers = [
                    (p, dist) for p, dist in related_papers if dist is not None and dist < 0.70
                ]
                if close_papers:
                    rag_lines = []
                    for idx, (p, dist) in enumerate(close_papers, 1):
                        sim_pct = int(round((1.0 - float(dist)) * 100))
                        p_summary = (p.abstract or "")[:400]
                        rag_lines.append(
                            f'[{idx}] "{p.title}" (Alaka: %{sim_pct})\nÖzet: {p_summary}'
                        )
                    rag_library_text = (
                        "\n\n--- KULLANICININ KÜTÜPHANESİNDEN ÇEKİLEN İLGİLİ DİĞER ÇALIŞMALAR (RAG BAĞLAMI) ---\n"
                        + "\n\n".join(rag_lines)
                    )
        except Exception:  # noqa: BLE001
            pass

    system_prompt = f"""Sen akademik bir araştırma asistanısın. Sana başlığı, özeti ve kütüphaneden anlamsal olarak çekilen ilgili bağlamları verilen şu bilimsel çalışma hakkında soruları yanıtlayacaksın.
Cevaplarını DAİMA samimi, net, bilimsel ciddiyette, markdown formatında ve Türkçe olarak ver.
Eğer varsa analiz bulgularına ve kullanıcının kütüphanesindeki ilgili diğer çalışmalara atıfta bulunarak karşılaştırma yap.
Eğer yanıt verilen bağlamda yer almıyorsa, bunu açıkça belirt.

Makale Başlığı: {title}
Makale Özeti:
{abstract}{analysis_text}{notes_text}{rag_library_text}"""

    convo = ""
    if history:
        lines = []
        for msg in history:
            role_label = "Kullanıcı" if msg.get("role") == "user" else "Asistan"
            lines.append(f"{role_label}: {msg.get('content', '')}")
        convo = "\n".join(lines) + "\n\n"
    user_msg = f"{convo}Kullanıcı: {question}"

    answer, _raw = ai_service._call_llm(
        system=system_prompt,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_CHAT,
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
    user_msg = f"Başlık: {title}\n\nSayfa metni:\n{body[: ai_service._WEB_SUMMARY_INPUT_CHARS]}"

    answer, _raw = ai_service._call_llm(
        system=system_prompt,
        user_msg=user_msg,
        max_tokens=ai_service.MAX_TOKENS_WEB_SUMMARY,
        user=user,
        expect_json=False,
    )
    if answer is None:
        return None
    return answer.strip() or None
