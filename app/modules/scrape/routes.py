"""Papers UI — the user-facing read view + per-paper actions.

Route layout:
    /papers/                      → Discover feed (default view)
    /papers/?view=favorites       → starred only
    /papers/?view=dismissed       → hidden bin (recovery)
    /papers/<id>                  → paper detail (read + notes panel)
    /papers/<id>/open             → mark seen and redirect to source URL
    /papers/<id>/favorite/toggle  → HTMX: flip star, swap card actions
    /papers/<id>/dismiss          → HTMX: hide from feed, swap to undo banner
    /papers/<id>/undismiss        → HTMX: put back
    /papers/<id>/notes            → HTMX: POST adds, GET lists (partial)
    /papers/notes/<note_id>       → HTMX: DELETE drops a note
    /papers/run                   → POST queue a one-off scrape
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape.service import (
    add_note,
    count_user_papers,
    delete_note,
    edit_note,
    get_note_for_user,
    get_user_paper,
    list_user_papers,
    mark_seen,
    set_dismissed,
    to_bibtex,
    toggle_favorite,
)

scrape_bp = Blueprint("scrape", __name__, template_folder="templates")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


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
    view = request.args.get("view", "discover")
    if view not in {"discover", "favorites", "dismissed", "all"}:
        view = "discover"
    q = request.args.get("q", "").strip()
    rows = list_user_papers(current_user, limit=100, view=view, q=q or None)
    counts = {
        "discover": count_user_papers(current_user, view="discover"),
        "favorites": count_user_papers(current_user, view="favorites"),
    }
    return render_template("scrape/feed.html", rows=rows, view=view, counts=counts, q=q)


def _get_similar_papers(link, limit=4):
    from sqlalchemy import desc

    from app.modules.scrape.models import UserPaper

    q = UserPaper.query.filter(
        UserPaper.user_id == link.user_id, UserPaper.id != link.id, UserPaper.dismissed_at.is_(None)
    )
    if link.matched_keyword:
        q = q.filter(UserPaper.matched_keyword == link.matched_keyword)
    similar = q.order_by(desc(UserPaper.created_at)).limit(limit).all()
    if len(similar) < limit:
        cat_list = link.paper.categories or []
        if cat_list:
            fallback_q = UserPaper.query.filter(
                UserPaper.user_id == link.user_id,
                UserPaper.id != link.id,
                UserPaper.dismissed_at.is_(None),
            )
            if link.matched_keyword:
                fallback_q = fallback_q.filter(UserPaper.matched_keyword != link.matched_keyword)
            others = fallback_q.order_by(desc(UserPaper.created_at)).limit(20).all()
            for o in others:
                if len(similar) >= limit:
                    break
                o_cats = o.paper.categories or []
                if any(c in o_cats for c in cat_list):
                    similar.append(o)
    return similar[:limit]


@scrape_bp.route("/<int:user_paper_id>")
@login_required
def detail(user_paper_id: int):
    """Paper detail with a 4-mode toggle (Original / TR / AI Analysis / RAG Chat).

    Mode comes from `?mode=original|tr|ai|chat`; default is "original". We pull
    the cached translation/analysis up front so the partial doesn't have
    to know about ai_service — anything missing just renders the "not yet"
    state plus a "Generate" button that hits the HTMX trigger endpoint.
    """
    from app.modules.scrape.ai_service import get_analysis, get_translation, is_ai_enabled

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    mark_seen(link)

    mode = request.args.get("mode", "original")
    if mode not in {"original", "tr", "ai", "chat"}:
        mode = "original"

    # Similar papers
    similar_papers = []
    if mode == "original":
        similar_papers = _get_similar_papers(link)

    # RAG Chat messages
    chat_messages = []
    if mode == "chat":
        from app.modules.scrape.models import PaperChatMessage

        chat_messages = (
            PaperChatMessage.query.filter_by(user_paper_id=link.id)
            .order_by(PaperChatMessage.created_at.asc())
            .all()
        )

    template = "scrape/_detail_content.html" if _is_htmx() else "scrape/detail.html"
    return render_template(
        template,
        r=link,
        mode=mode,
        ai_enabled=is_ai_enabled(),
        translation=get_translation(link.paper) if mode == "tr" else None,
        analysis=get_analysis(link.paper) if mode == "ai" else None,
        chat_messages=chat_messages,
        similar_papers=similar_papers,
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
    if not is_ai_enabled():
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

    # 3. Call Claude to get answer
    answer = ask_paper(link.paper, question, history=history)
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

    # 5. Return both messages for HTMX to append to the chat log
    return f"""
    <div class="chat-message chat-message--user">{user_msg.content}</div>
    <div class="chat-message chat-message--assistant">{assistant_msg.content}</div>
    """


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
    if not is_ai_enabled():
        return render_template("scrape/_ai_disabled.html", kind="analysis")
    force = request.args.get("force") == "1"
    analysis = get_or_generate_analysis(link.paper, force=force)
    log_action(
        "paper.analysis_generated",
        entity_type="paper",
        entity_id=str(link.paper.id),
        changes={"force": force, "ok": analysis is not None},
    )
    return render_template("scrape/_ai_analysis.html", r=link, analysis=analysis)


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
    if not is_ai_enabled():
        return render_template("scrape/_ai_disabled.html", kind="translation")
    force = request.args.get("force") == "1"
    translation = get_or_generate_translation(link.paper, force=force)
    log_action(
        "paper.translation_generated",
        entity_type="paper",
        entity_id=str(link.paper.id),
        changes={"force": force, "ok": translation is not None},
    )
    return render_template("scrape/_ai_translation.html", r=link, translation=translation)


# ----------------------------------------------------------------------------
# Per-paper actions — HTMX swaps the card
# ----------------------------------------------------------------------------


@scrape_bp.route("/<int:user_paper_id>/cite", methods=["GET"])
@login_required
def cite_route(user_paper_id: int):
    """Plain-text BibTeX entry for the paper. Browser-side JS copies it to
    the clipboard; we also serve it as a download for the rare power user
    who wants the .bib file directly via ?download=1."""
    from flask import Response

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    bib = to_bibtex(link.paper)
    if request.args.get("download") == "1":
        filename = f"{link.paper.source}-{link.paper.external_id}.bib"
        return Response(
            bib,
            mimetype="application/x-bibtex",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return Response(bib, mimetype="text/plain; charset=utf-8")


@scrape_bp.route("/<int:user_paper_id>/open", methods=["POST"])
@login_required
def open_paper(user_paper_id: int):
    """Mark seen and redirect to source URL (non-HTMX, full nav)."""
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        flash(_("Paper not found."), "danger")
        return redirect(url_for("scrape.feed"))
    mark_seen(link)
    target = link.paper.url or url_for("scrape.feed")
    return redirect(target)


@scrape_bp.route("/<int:user_paper_id>/favorite/toggle", methods=["POST"])
@login_required
def toggle_favorite_route(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    is_now_fav = toggle_favorite(link)
    log_action(
        "paper.favorite_toggled",
        entity_type="user_paper",
        entity_id=str(link.id),
        changes={"is_favorite": is_now_fav},
    )
    if _is_htmx():
        # Detail-page button uses ?as=button to swap just itself; feed cards
        # swap the whole card so the badge ("Favorite"/"New") refreshes too.
        if request.args.get("as") == "button":
            return render_template("scrape/_favorite_button.html", r=link)
        return _render_card(link)
    flash(_("Saved to favorites.") if is_now_fav else _("Removed from favorites."), "success")
    return redirect(request.referrer or url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/dismiss", methods=["POST"])
@login_required
def dismiss(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    set_dismissed(link, True)
    log_action("paper.dismissed", entity_type="user_paper", entity_id=str(link.id))
    if _is_htmx():
        # Render an undo banner that replaces the card in-place; clicking
        # it un-dismisses without losing the slot.
        return render_template("scrape/_dismissed_undo.html", r=link)
    flash(_("Hidden from your feed."), "info")
    return redirect(url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/undismiss", methods=["POST"])
@login_required
def undismiss(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    set_dismissed(link, False)
    log_action("paper.undismissed", entity_type="user_paper", entity_id=str(link.id))
    if _is_htmx():
        return _render_card(link)
    return redirect(url_for("scrape.feed"))


# ----------------------------------------------------------------------------
# Notes — HTMX add/delete inside paper detail
# ----------------------------------------------------------------------------


@scrape_bp.route("/<int:user_paper_id>/notes", methods=["POST"])
@login_required
def add_note_route(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    body = request.form.get("body", "")
    tag = request.form.get("tag", "")
    note = add_note(link, body, tag=tag)
    if note is None:
        # Empty body — re-render the list unchanged with a small inline message
        if _is_htmx():
            return render_template(
                "scrape/_notes_list.html",
                r=link,
                flash_msg=_("Empty notes are not saved."),
                flash_kind="danger",
            )
        flash(_("Empty notes are not saved."), "danger")
        return redirect(url_for("scrape.detail", user_paper_id=link.id))
    log_action(
        "paper.note_added",
        entity_type="paper_note",
        entity_id=str(note.id),
        changes={"tag": note.tag},
    )
    if _is_htmx():
        return render_template("scrape/_notes_list.html", r=link)
    return redirect(url_for("scrape.detail", user_paper_id=link.id))


@scrape_bp.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note_route(note_id: int):
    note = get_note_for_user(current_user, note_id)
    if note is None:
        abort(404)
    parent = note.user_paper
    delete_note(note)
    log_action("paper.note_deleted", entity_type="paper_note", entity_id=str(note_id))
    if _is_htmx():
        return render_template("scrape/_notes_list.html", r=parent)
    return redirect(url_for("scrape.detail", user_paper_id=parent.id))


@scrape_bp.route("/notes/<int:note_id>/view", methods=["GET"])
@login_required
def view_note_route(note_id: int):
    """Read-mode partial for a single note. Used by the inline edit form's
    Cancel button to swap back to the original card."""
    note = get_note_for_user(current_user, note_id)
    if note is None:
        abort(404)
    return render_template("scrape/_note_view.html", n=note)


@scrape_bp.route("/notes/<int:note_id>/edit", methods=["GET", "POST"])
@login_required
def edit_note_route(note_id: int):
    """Inline edit. GET returns the textarea swap; POST persists and swaps
    back to the read view. Empty body is rejected the same way add_note
    does — validation in one place."""
    note = get_note_for_user(current_user, note_id)
    if note is None:
        abort(404)

    if request.method == "GET":
        return render_template("scrape/_note_edit.html", n=note)

    body = request.form.get("body", "")
    tag = request.form.get("tag", "")
    ok = edit_note(note, body, tag=tag)
    if not ok:
        return render_template(
            "scrape/_note_edit.html",
            n=note,
            flash_msg=_("Empty notes are not saved."),
            flash_kind="danger",
        )
    log_action(
        "paper.note_edited",
        entity_type="paper_note",
        entity_id=str(note.id),
        changes={"tag": note.tag},
    )
    return render_template("scrape/_note_view.html", n=note)


# ----------------------------------------------------------------------------
# Manual scrape
# ----------------------------------------------------------------------------


@scrape_bp.route("/run", methods=["POST"])
@login_required
def run_now():
    from app.tasks.scrape_tasks import run_for_user

    async_result = run_for_user.delay(current_user.id)
    log_action(
        "scrape.manual_run",
        entity_type="user",
        entity_id=str(current_user.id),
        changes={"task_id": getattr(async_result, "id", None)},
    )
    if _is_htmx():
        return render_template("scrape/_scrape_spinner.html", task_id=async_result.id)
    flash(_("Scrape queued — papers will appear here once the worker finishes."), "info")
    return redirect(url_for("scrape.feed"))


@scrape_bp.route("/status/<task_id>", methods=["GET"])
@login_required
def scrape_status_poll(task_id: str):
    from celery.result import AsyncResult
    from flask import Response

    from app.modules.academic.service import list_user_keywords

    res = AsyncResult(task_id)
    if res.ready():
        has_interests = bool(list_user_keywords(current_user))
        response = Response(
            render_template("scrape/_scraper_widget.html", has_interests=has_interests)
        )
        response.headers["HX-Refresh"] = "true"
        return response
    else:
        return render_template("scrape/_scrape_spinner.html", task_id=task_id)


@scrape_bp.route("/bulk-action", methods=["POST"])
@login_required
def bulk_action():
    from datetime import UTC, datetime

    from app.extensions import db
    from app.modules.scrape.models import UserPaper

    paper_ids = request.form.getlist("paper_ids")
    action = request.form.get("action")

    if not paper_ids or not action:
        flash(_("No papers selected or action specified."), "warning")
        return redirect(url_for("scrape.feed"))

    try:
        paper_ids = [int(pid) for pid in paper_ids]
    except ValueError:
        flash(_("Invalid paper selection."), "danger")
        return redirect(url_for("scrape.feed"))

    links = UserPaper.query.filter(
        UserPaper.id.in_(paper_ids), UserPaper.user_id == current_user.id
    ).all()

    if not links:
        flash(_("No matching papers found."), "warning")
        return redirect(url_for("scrape.feed"))

    if action == "favorite":
        for link in links:
            link.is_favorite = True
        db.session.commit()
        flash(_("Selected papers added to favorites."), "success")
    elif action == "read_later":
        for link in links:
            link.read_later = True
        db.session.commit()
        flash(_("Selected papers marked as Read Later."), "success")
    elif action == "dismiss":
        for link in links:
            link.dismissed_at = datetime.now(UTC)
        db.session.commit()
        flash(_("Selected papers hidden."), "info")
    else:
        flash(_("Unknown action."), "danger")

    log_action(
        "paper.bulk_action",
        entity_type="user_paper",
        entity_id="bulk",
        changes={"count": len(links), "action": action},
    )

    return redirect(request.referrer or url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/read-later/toggle", methods=["POST"])
@login_required
def toggle_read_later_route(user_paper_id: int):
    from app.modules.scrape.service import toggle_read_later

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    is_now_rl = toggle_read_later(link)
    log_action(
        "paper.read_later_toggled",
        entity_type="user_paper",
        entity_id=str(link.id),
        changes={"read_later": is_now_rl},
    )
    if _is_htmx():
        if request.args.get("as") == "button":
            return render_template("scrape/_read_later_button.html", r=link)
        return _render_card(link)
    flash(_("Added to Read Later.") if is_now_rl else _("Removed from Read Later."), "success")
    return redirect(request.referrer or url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/export-notes", methods=["GET"])
@login_required
def export_notes_route(user_paper_id: int):
    """Export all notes for a specific user paper as Markdown file download."""
    from datetime import datetime

    from flask import Response

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)

    title = link.paper.title or "Untitled"
    external_id = link.paper.external_id or ""

    md_content = f"# Notes on: {title}\n"
    md_content += f"- Source: {link.paper.source} ({external_id})\n"
    md_content += f"- Exported on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    md_content += "## Notes\n\n"

    if not link.notes:
        md_content += "_No notes taken for this paper._\n"
    else:
        for n in link.notes:
            tag_header = f"### [{n.tag.upper()}]" if n.tag else "### [Note]"
            md_content += f"{tag_header} ({n.created_at.strftime('%Y-%m-%d %H:%M')})\n"
            md_content += f"{n.body}\n\n"

    filename = f"notes-{link.paper.source}-{external_id}.md".replace("/", "-")
    return Response(
        md_content,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
