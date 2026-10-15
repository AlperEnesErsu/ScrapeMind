"""Per-paper actions (cite, favourite, dismiss), notes, bulk actions and export."""

from __future__ import annotations

import structlog
from flask import abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape import routes
from app.modules.scrape.routes import scrape_bp
from app.modules.scrape.service import (
    add_note,
    delete_note,
    edit_note,
    get_note_for_user,
    get_user_paper,
    mark_seen,
    set_dismissed,
    to_bibtex,
    toggle_favorite,
)

logger = structlog.get_logger()


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
    if routes._is_htmx():
        # Detail-page button uses ?as=button to swap just itself; feed cards
        # swap the whole card so the badge ("Favorite"/"New") refreshes too.
        if request.args.get("as") == "button":
            return render_template("scrape/_favorite_button.html", r=link)
        return routes._render_card(link)
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
    if routes._is_htmx():
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
    if routes._is_htmx():
        return routes._render_card(link)
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
        if routes._is_htmx():
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
    if routes._is_htmx():
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
    if routes._is_htmx():
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
    if routes._is_htmx():
        if request.args.get("as") == "button":
            return render_template("scrape/_read_later_button.html", r=link)
        return routes._render_card(link)
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
