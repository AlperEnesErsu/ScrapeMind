"""Write endpoints for API v1 — per-paper state and notes.

REST-shaped and idempotent on purpose: the web UI toggles (a click flips the
flag), but an API client that retries a request must not flip state back. So
each flag gets PUT (set) / DELETE (unset) instead of one toggle endpoint.

Ownership is enforced by the service layer (`get_user_paper` /
`get_note_for_user` return None for someone else's row); a miss and a
mismatch both surface as 404 so the API never confirms that an id exists.
"""

from __future__ import annotations

from flask import g, request

from app.api.v1 import api_v1_bp
from app.api.v1.decorators import jwt_required
from app.api.v1.errors import error_response
from app.api.v1.serializers import note_to_dict, user_paper_to_dict
from app.core.audit.middleware import log_action
from app.modules.scrape.service import (
    add_note,
    delete_note,
    edit_note,
    get_note_for_user,
    get_user_paper,
    mark_seen,
    set_dismissed,
    set_favorite,
    set_read_later,
)

_NOT_FOUND = ("not_found", "Paper not found in your library.")


def _payload():
    return request.get_json(silent=True) or request.form


def _own_paper(user_paper_id: int):
    """(link, error). Ownership mismatch is reported as 404, not 403."""
    link = get_user_paper(g.api_user, user_paper_id)
    if link is None:
        return None, error_response(404, *_NOT_FOUND)
    return link, None


def _audit(action: str, link) -> None:
    log_action(
        action,
        entity_type="user_paper",
        entity_id=str(link.id),
        user_id=g.api_user.id,
    )


# ---------------------------------------------------------------------------
# Per-paper flags
# ---------------------------------------------------------------------------


@api_v1_bp.route("/me/papers/<int:user_paper_id>/favorite", methods=["PUT", "DELETE"])
@jwt_required
def set_paper_favorite(user_paper_id: int):
    link, err = _own_paper(user_paper_id)
    if err is not None:
        return err
    set_favorite(link, request.method == "PUT")
    _audit("paper.favorite_toggled", link)
    return {"data": user_paper_to_dict(link)}


@api_v1_bp.route("/me/papers/<int:user_paper_id>/read-later", methods=["PUT", "DELETE"])
@jwt_required
def set_paper_read_later(user_paper_id: int):
    link, err = _own_paper(user_paper_id)
    if err is not None:
        return err
    set_read_later(link, request.method == "PUT")
    _audit("paper.read_later_toggled", link)
    return {"data": user_paper_to_dict(link)}


@api_v1_bp.route("/me/papers/<int:user_paper_id>/dismissed", methods=["PUT", "DELETE"])
@jwt_required
def set_paper_dismissed(user_paper_id: int):
    link, err = _own_paper(user_paper_id)
    if err is not None:
        return err
    dismissed = request.method == "PUT"
    set_dismissed(link, dismissed)
    _audit("paper.dismissed" if dismissed else "paper.undismissed", link)
    return {"data": user_paper_to_dict(link)}


@api_v1_bp.route("/me/papers/<int:user_paper_id>/seen", methods=["POST"])
@jwt_required
def mark_paper_seen(user_paper_id: int):
    """Idempotent by nature — seen_at is only stamped the first time."""
    link, err = _own_paper(user_paper_id)
    if err is not None:
        return err
    mark_seen(link)
    return {"data": user_paper_to_dict(link)}


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


@api_v1_bp.route("/me/papers/<int:user_paper_id>/notes", methods=["GET"])
@jwt_required
def list_notes(user_paper_id: int):
    link, err = _own_paper(user_paper_id)
    if err is not None:
        return err
    return {"data": [note_to_dict(n) for n in link.notes]}


@api_v1_bp.route("/me/papers/<int:user_paper_id>/notes", methods=["POST"])
@jwt_required
def create_note(user_paper_id: int):
    link, err = _own_paper(user_paper_id)
    if err is not None:
        return err
    data = _payload()
    note = add_note(link, data.get("body") or "", data.get("tag"))
    if note is None:
        return error_response(422, "empty_body", "Note body must not be empty.")
    _audit("paper.note_added", link)
    # An unrecognised tag is dropped rather than rejected (service contract);
    # the response echoes what was actually stored.
    return {"data": note_to_dict(note)}, 201


@api_v1_bp.route("/notes/<int:note_id>", methods=["PATCH"])
@jwt_required
def update_note(note_id: int):
    note = get_note_for_user(g.api_user, note_id)
    if note is None:
        return error_response(404, "not_found", "Note not found.")
    data = _payload()
    # PATCH semantics: an omitted field keeps its current value.
    body = data.get("body")
    tag = data.get("tag", note.tag)
    if not edit_note(note, note.body if body is None else body, tag):
        return error_response(422, "empty_body", "Note body must not be empty.")
    log_action(
        "paper.note_edited",
        entity_type="paper_note",
        entity_id=str(note.id),
        user_id=g.api_user.id,
    )
    return {"data": note_to_dict(note)}


@api_v1_bp.route("/notes/<int:note_id>", methods=["DELETE"])
@jwt_required
def remove_note(note_id: int):
    note = get_note_for_user(g.api_user, note_id)
    if note is None:
        return error_response(404, "not_found", "Note not found.")
    delete_note(note)
    log_action(
        "paper.note_deleted",
        entity_type="paper_note",
        entity_id=str(note_id),
        user_id=g.api_user.id,
    )
    return {"deleted": True}
