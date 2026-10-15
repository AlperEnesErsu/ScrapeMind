"""Paper feed, detail, add-link, similar papers, chat and AI generation handlers."""

from __future__ import annotations

import structlog
from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape import routes
from app.modules.scrape.routes import scrape_bp
from app.modules.scrape.service import (
    count_user_papers,
    get_user_paper,
    list_user_papers,
    mark_seen,
)

logger = structlog.get_logger()


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _widget_ctx() -> dict:
    """Everything `_scraper_widget.html` needs when rendered standalone.

    The widget used to read `active_sources` from a global context processor
    that returned the *deployment's* enabled sources — so a user who had muted
    arXiv still saw an "arXiv" badge. `scan_status_context` supplies that
    user's effective set, plus the last/next scan times.
    """
    from app.modules.academic.service import list_user_keywords
    from app.modules.scrape.service import scan_status_context

    return {
        "has_interests": bool(list_user_keywords(current_user)),
        **scan_status_context(current_user),
    }


def _render_card(link, *, flash_msg=None, flash_kind=None):
    """Render a single paper card — swap target for HTMX action handlers."""
    return render_template(
        "scrape/_paper_card.html",
        r=link,
        flash_msg=flash_msg,
        flash_kind=flash_kind,
    )


# ----------------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------------


@scrape_bp.route("/")
@login_required
def feed():
    from app.modules.academic.service import list_user_keywords
    from app.modules.scrape.service import scan_status_context, sources_card_context

    view = request.args.get("view", "discover")
    if view not in {"discover", "favorites", "dismissed", "all"}:
        view = "discover"
    q = request.args.get("q", "").strip()
    semantic = request.args.get("semantic") == "1"
    rows = list_user_papers(current_user, limit=100, view=view, q=q or None, semantic=semantic)
    counts = {
        "discover": count_user_papers(current_user, view="discover"),
        "favorites": count_user_papers(current_user, view="favorites"),
    }
    return render_template(
        "scrape/feed.html",
        rows=rows,
        view=view,
        counts=counts,
        q=q,
        semantic=semantic,
        user_keywords=list_user_keywords(current_user),
        # For the sidebar `_sources_card.html`, which shows the next-scan time
        # and the custom-feed counts. The scrape control itself lives in the
        # dashboard header, not here.
        **scan_status_context(current_user),
        **sources_card_context(current_user),
    )


@scrape_bp.route("/add-link", methods=["POST"])
@login_required
def add_link_route():
    """Manually add a web link to the user's library."""
    url = request.form.get("url", "").strip()
    if not url:
        if request.headers.get("HX-Request"):
            return (
                f'<div class="alert alert-warning py-2 mb-0 small">{_("Please enter a valid URL.")}</div>',
                400,
            )
        flash(_("Please enter a valid URL."), "warning")
        return redirect(url_for("scrape.feed"))

    try:
        from app.modules.scrape.service import add_paper_from_url

        link, created = add_paper_from_url(current_user, url)
        # Actor passed explicitly so core's audit middleware needs no
        # module-specific fallback (app/core never bends for app/modules).
        log_action(
            "add_paper_from_url",
            entity_type="paper",
            entity_id=link.paper_id,
            user_id=current_user.id,
        )
        msg = (
            _("Link added successfully.") if created else _("Link already exists in your library.")
        )
        if request.headers.get("HX-Request"):
            return render_template(
                "scrape/_paper_card.html",
                r=link,
                flash_msg=msg,
                flash_kind="success" if created else "info",
            )

        flash(msg, "success" if created else "info")
        return redirect(url_for("scrape.feed"))
    except ValueError as e:
        err_msg = str(e)
        if request.headers.get("HX-Request"):
            return f'<div class="alert alert-danger py-2 mb-0 small">{err_msg}</div>', 400
        flash(err_msg, "danger")
        return redirect(url_for("scrape.feed"))
    except Exception as e:
        logger.error("add_link_failed", user_id=current_user.id, url=url, error=str(e))
        err_msg = _("Failed to process URL. Please check the URL and try again.")
        if request.headers.get("HX-Request"):
            return f'<div class="alert alert-danger py-2 mb-0 small">{err_msg}</div>', 500
        flash(err_msg, "danger")
        return redirect(url_for("scrape.feed"))


def _get_internal_similar(link, limit=4):
    """Papers already in the user's own library that resemble this one.

    Uses pgvector cosine distance if the current paper has an embedding,
    attaching a `similarity_score` (0.0 .. 1.0) to each result.
    Falls back to matched keyword and category heuristics when embeddings
    are absent or return fewer than `limit` results.
    """
    from sqlalchemy import desc

    from app.extensions import db
    from app.modules.scrape.models import Paper, UserPaper

    similar = []
    seen_ids = set()

    # 1. Vector similarity (pgvector)
    if link.paper and link.paper.embedding is not None and link.paper.embedding_model:
        dist_col = Paper.embedding.cosine_distance(link.paper.embedding).label("dist")
        vector_matches = (
            db.session.query(UserPaper, dist_col)
            .join(Paper, UserPaper.paper_id == Paper.id)
            .filter(
                UserPaper.user_id == link.user_id,
                UserPaper.id != link.id,
                UserPaper.dismissed_at.is_(None),
                Paper.embedding.is_not(None),
                # Two stored vectors: comparable only if one model made both.
                Paper.embedding_model == link.paper.embedding_model,
            )
            .order_by(dist_col.asc())
            .limit(limit)
            .all()
        )
        for up, dist in vector_matches:
            if dist is not None and dist < 0.85:
                up.similarity_score = max(0.0, min(1.0, round(1.0 - float(dist), 2)))
                similar.append(up)
                seen_ids.add(up.id)

    # 2. Heuristic fallback (keyword/category) if we still need more items
    if len(similar) < limit:
        q = UserPaper.query.filter(
            UserPaper.user_id == link.user_id,
            UserPaper.id != link.id,
            UserPaper.dismissed_at.is_(None),
        )
        if seen_ids:
            q = q.filter(UserPaper.id.notin_(seen_ids))
        if link.matched_keyword:
            kw_matches = (
                q.filter(UserPaper.matched_keyword == link.matched_keyword)
                .order_by(desc(UserPaper.created_at))
                .limit(limit - len(similar))
                .all()
            )
            for m in kw_matches:
                similar.append(m)
                seen_ids.add(m.id)

    if len(similar) < limit:
        cat_list = link.paper.categories or [] if link.paper else []
        if cat_list:
            fallback_q = UserPaper.query.filter(
                UserPaper.user_id == link.user_id,
                UserPaper.id != link.id,
                UserPaper.dismissed_at.is_(None),
            )
            if seen_ids:
                fallback_q = fallback_q.filter(UserPaper.id.notin_(seen_ids))
            others = fallback_q.order_by(desc(UserPaper.created_at)).limit(20).all()
            for o in others:
                if len(similar) >= limit:
                    break
                o_cats = o.paper.categories or []
                if any(c in o_cats for c in cat_list):
                    similar.append(o)
                    seen_ids.add(o.id)

    return similar[:limit]


def _get_external_similar(link, limit=5):
    """Recommendations from the wider literature via the Semantic Scholar
    Recommendations API. Network I/O + often rate-limited, so this is only
    ever called from the lazy-loaded /similar endpoint, never inline in the
    detail page. Returns [] when the paper has no DOI/S2 id or the API declines.
    """
    from app.modules.scrape.sources.semantic_scholar_source import fetch_similar_papers

    paper_id_or_doi = link.paper.doi or (
        link.paper.external_id if link.paper.source == "semantic_scholar" else None
    )
    if not paper_id_or_doi:
        return []
    try:
        return fetch_similar_papers(paper_id_or_doi, limit=limit)
    except Exception:  # noqa: BLE001
        return []


@scrape_bp.route("/<int:user_paper_id>")
@login_required
def detail(user_paper_id: int):
    """Paper detail with a 4-mode toggle (Original / TR / AI Analysis / RAG Chat).

    Mode comes from `?mode=original|tr|ai|chat`; default is "original". We pull
    the cached translation/analysis up front so the partial doesn't have
    to know about ai_service — anything missing just renders the "not yet"
    state plus a "Generate" button that hits the HTMX trigger endpoint.
    """
    from app.modules.scrape.ai_service import (
        get_analysis,
        get_translation,
        get_video_summary,
        is_ai_enabled,
    )

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    mark_seen(link)

    mode = request.args.get("mode", "original")
    if mode not in {"original", "tr", "ai", "chat", "graph"}:
        mode = "original"

    # RAG Chat messages
    chat_messages = []
    if mode == "chat":
        from app.modules.scrape.models import PaperChatMessage

        chat_messages = (
            PaperChatMessage.query.filter_by(user_paper_id=link.id)
            .order_by(PaperChatMessage.created_at.asc())
            .all()
        )

    template = "scrape/_detail_content.html" if routes._is_htmx() else "scrape/detail.html"
    return render_template(
        template,
        r=link,
        mode=mode,
        ai_enabled=is_ai_enabled(current_user),
        translation=get_translation(link.paper) if mode == "tr" else None,
        analysis=get_analysis(link.paper) if mode == "ai" else None,
        # Cache-only lookup — a GET never attempts generation, so there is no
        # "no transcript" signal here, only "cached" vs "not generated yet".
        # `no_transcript` (the route below sets it) is a transient result of
        # a POST attempt, not persisted state, so it's always False on load.
        summary=(
            get_video_summary(link.paper) if mode == "ai" and link.paper.kind == "video" else None
        ),
        no_transcript=False,
        chat_messages=chat_messages,
    )


@scrape_bp.route("/<int:user_paper_id>/citation-graph", methods=["GET"])
@login_required
def citation_graph(user_paper_id: int):
    """Return JSON citation graph (references + citations) for this paper."""
    from app.modules.scrape.citation_service import get_citation_graph_for_user

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)

    graph_data = get_citation_graph_for_user(link.paper, current_user, link.id)
    return jsonify(graph_data)


@scrape_bp.route("/<int:user_paper_id>/citation-graph/add", methods=["POST"])
@login_required
def add_citation_paper(user_paper_id: int):
    """Add a paper found in the citation graph into user's library."""
    from datetime import UTC, datetime

    from app.modules.scrape.doi import normalize_doi
    from app.modules.scrape.service import link_user_paper, upsert_paper
    from app.modules.scrape.sources.payload import PaperPayload

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)

    data = request.get_json(silent=True) or request.form
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"status": "error", "message": "Title is required"}), 400

    raw_doi = data.get("doi")
    norm_doi = normalize_doi(raw_doi) if raw_doi else None

    raw_year = data.get("year")
    pub_date = None
    if raw_year:
        try:
            pub_date = datetime(int(raw_year), 1, 1, tzinfo=UTC)
        except (ValueError, TypeError):
            pass

    authors_val = data.get("authors")
    if isinstance(authors_val, str):
        authors = [a.strip() for a in authors_val.split(",") if a.strip()]
    elif isinstance(authors_val, list):
        authors = [str(a).strip() for a in authors_val if str(a).strip()]
    else:
        authors = []

    ext_id = data.get("id") or norm_doi or f"cit_{abs(hash(title)) % 10000000}"
    url = data.get("url") or (f"https://doi.org/{norm_doi}" if norm_doi else None)

    payload = PaperPayload(
        source="openalex",
        external_id=str(ext_id),
        title=title,
        abstract=None,
        authors=authors,
        url=url,
        pdf_url=None,
        published_at=pub_date,
        categories=[data.get("venue")] if data.get("venue") else [],
        kind="paper",
        doi=norm_doi,
    )

    paper = upsert_paper(payload)
    new_link, created = link_user_paper(current_user, paper, matched_keyword="citation_graph")

    from app.modules.scrape.embedding_service import needs_embedding

    if needs_embedding(paper):
        try:
            from app.tasks.embedding_tasks import embed_paper_task

            embed_paper_task.delay(paper.id)
        except Exception:
            pass

    return jsonify(
        {
            "status": "ok",
            "created": created,
            "user_paper_id": new_link.id,
            "paper_id": paper.id,
        }
    )


@scrape_bp.route("/<int:user_paper_id>/similar", methods=["GET"])
@login_required
def similar(user_paper_id: int):
    """HTMX-loaded Similar Papers panel — internal (user's own library) plus
    external Semantic Scholar recommendations. Lazy so the S2 network call
    never blocks the detail page render."""
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    return render_template(
        "scrape/_similar_papers.html",
        r=link,
        internal=routes._get_internal_similar(link, limit=4),
        external=routes._get_external_similar(link, limit=5),
    )


@scrape_bp.route("/<int:user_paper_id>/chat", methods=["POST"])
@login_required
def send_chat_message(user_paper_id: int):
    """RAG Chat endpoint to ask Claude a question about the paper."""
    from app.extensions import db
    from app.modules.scrape.ai_service import ask_paper, is_ai_enabled
    from app.modules.scrape.models import PaperChatMessage

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    if not is_ai_enabled(current_user):
        abort(400, "AI not enabled")

    question = request.form.get("message", "").strip()
    if not question:
        return "", 204

    # 1. Save user question to DB
    user_msg = PaperChatMessage(user_paper_id=link.id, role="user", content=question)
    db.session.add(user_msg)
    db.session.commit()

    # 2. Get chat history for Claude context
    history_rows = (
        PaperChatMessage.query.filter_by(user_paper_id=link.id)
        .order_by(PaperChatMessage.created_at.asc())
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in history_rows[:-1]]

    # 3. Call the resolved LLM to get an answer
    answer = ask_paper(link.paper, question, history=history, user=current_user)
    if not answer:
        answer = _(
            "Claude did not return a response. Please check your credentials or try again later."
        )

    # 4. Save Claude response to DB
    assistant_msg = PaperChatMessage(user_paper_id=link.id, role="assistant", content=answer)
    db.session.add(assistant_msg)
    db.session.commit()

    log_action(
        "paper.chat_message_sent",
        entity_type="user_paper",
        entity_id=str(link.id),
        changes={"question_length": len(question), "answer_length": len(answer)},
    )

    # 5. Return both messages for HTMX to append to the chat log. Render via
    #    Jinja so the user's question and Claude's answer are auto-escaped —
    #    interpolating them into raw HTML here would be a stored-XSS hole
    #    (a question containing <script> would execute on every reload).
    return render_template(
        "scrape/_chat_message.html", role="user", content=user_msg.content
    ) + render_template(
        "scrape/_chat_message.html", role="assistant", content=assistant_msg.content
    )


@scrape_bp.route("/<int:user_paper_id>/analysis", methods=["POST"])
@login_required
def generate_analysis_route(user_paper_id: int):
    """HTMX: trigger Claude analysis. Swaps the AI mode panel with the
    rendered result. `?force=1` re-runs the cache."""
    from app.modules.scrape.ai_service import (
        get_or_generate_analysis,
        is_ai_enabled,
    )

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    if not is_ai_enabled(current_user):
        return render_template("scrape/_ai_disabled.html", kind="analysis")
    force = request.args.get("force") == "1"
    analysis = get_or_generate_analysis(link.paper, force=force, user=current_user)
    log_action(
        "paper.analysis_generated",
        entity_type="paper",
        entity_id=str(link.paper.id),
        changes={"force": force, "ok": analysis is not None},
    )
    return render_template("scrape/_ai_analysis.html", r=link, analysis=analysis)


@scrape_bp.route("/<int:user_paper_id>/video-summary", methods=["POST"])
@login_required
def generate_video_summary_route(user_paper_id: int):
    """HTMX: fetch this video's transcript (yt-dlp subprocess) and summarize
    it via the resolved LLM. Swaps the video-summary panel with the result.
    `?force=1` re-fetches the transcript and re-runs the cache.

    Cost/latency note: unlike generate_analysis_route (one LLM call),
    fetch_transcript shells out to yt-dlp (`_YTDLP_TIMEOUT = 30`s in
    youtube_channel_source.py) *before* the LLM call, both blocking this web
    worker synchronously — a request here can take ~10-30s longer than the
    plain analysis route. That's the same "no Celery hop for a single-paper,
    user-triggered action" trade-off the analysis route already accepts, just
    a slower instance of it; the template's hx-disabled-elt + hx-indicator
    are what keep a user from firing a second yt-dlp subprocess by
    double-clicking while the first is still running.
    """
    from app.modules.scrape.ai_service import (
        generate_video_summary,
        get_video_summary,
        is_ai_enabled,
    )
    from app.modules.scrape.sources.youtube_channel_source import fetch_transcript

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    if not is_ai_enabled(current_user):
        return render_template("scrape/_ai_disabled.html", kind="video_summary")

    force = request.args.get("force") == "1"
    if not force:
        cached = get_video_summary(link.paper)
        if cached is not None:
            # Cache hit — never shell out to yt-dlp just to throw the
            # transcript away.
            return render_template(
                "scrape/_video_summary.html",
                r=link,
                summary=cached,
                no_transcript=False,
                ai_enabled=True,
            )

    transcript = fetch_transcript(link.paper.external_id)
    summary = generate_video_summary(link.paper, transcript, user=current_user)
    # No captions vs. a generation failure both come back as None from
    # generate_video_summary — distinguish them so the template can tell the
    # user "this video has no captions" instead of a generic retry prompt.
    no_transcript = summary is None and not transcript
    log_action(
        "paper.video_summary_generated",
        entity_type="paper",
        entity_id=str(link.paper.id),
        changes={"force": force, "ok": summary is not None, "no_transcript": no_transcript},
    )
    return render_template(
        "scrape/_video_summary.html",
        r=link,
        summary=summary,
        no_transcript=no_transcript,
        ai_enabled=True,
    )


@scrape_bp.route("/<int:user_paper_id>/translate", methods=["POST"])
@login_required
def generate_translation_route(user_paper_id: int):
    """HTMX: trigger Claude translation. Swaps the TR mode panel with the
    rendered title+abstract translation."""
    from app.modules.scrape.ai_service import (
        get_or_generate_translation,
        is_ai_enabled,
    )

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    if not is_ai_enabled(current_user):
        return render_template("scrape/_ai_disabled.html", kind="translation")
    force = request.args.get("force") == "1"
    translation = get_or_generate_translation(link.paper, force=force, user=current_user)
    log_action(
        "paper.translation_generated",
        entity_type="paper",
        entity_id=str(link.paper.id),
        changes={"force": force, "ok": translation is not None},
    )
    return render_template("scrape/_ai_translation.html", r=link, translation=translation)
