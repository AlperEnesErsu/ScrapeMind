"""Markup contract of the paper detail page that the browser audit relies on.

The detail page's mode toggle showed its current mode only by a background
tint, which a screen reader does not announce; and the page was outside the UI
audit entirely until it was found overflowing at 280px. These pin the parts a
template edit could quietly undo.
"""

from __future__ import annotations

import re
import uuid

import pytest

from app.modules.scrape.models import Paper, UserPaper


@pytest.fixture
def link_id(app, db, auth_client):
    _, uid = auth_client
    with app.app_context():
        paper = Paper(
            source="manual",
            external_id=f"detail-a11y-{uuid.uuid4().hex}",
            title="Detail page accessibility fixture",
            abstract=r"A gain of $3.58\%$ in the abstract.",
        )
        db.session.add(paper)
        db.session.commit()
        link = UserPaper(user_id=uid, paper_id=paper.id)
        db.session.add(link)
        db.session.commit()
        return link.id


@pytest.mark.parametrize("mode", ["original", "tr", "ai", "chat", "graph"])
def test_mode_toggle_marks_exactly_the_current_mode(auth_client, link_id, mode):
    client, _ = auth_client
    html = client.get(f"/papers/{link_id}?mode={mode}").data.decode("utf-8")
    current = re.findall(r'<a [^>]*aria-current="page"[^>]*>', html, flags=re.S)
    assert len(current) == 1, "one mode, and only one, is announced as current"
    assert f"mode={mode}" in current[0]
    assert "mode-toggle__btn--active" in current[0]


def test_note_form_footer_wraps(auth_client, link_id):
    """Four tag pills and a Save button are wider than a 280px phone; without
    wrapping the button pushed the page sideways."""
    client, _ = auth_client
    html = client.get(f"/papers/{link_id}").data.decode("utf-8")
    foot = re.search(r'class="note-add__foot[^"]*"', html)
    assert foot and "flex-wrap" in foot.group(0)
