"""Markup contract of the paper detail page that the browser audit relies on.

The detail page's mode toggle showed its current mode only by a background
tint, which a screen reader does not announce; and the page was outside the UI
audit entirely until it was found overflowing at 280px. These pin the parts a
template edit could quietly undo.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

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


@pytest.fixture
def ai_configured(app, monkeypatch):
    """Ask mode draws its form only when a model is configured. A GET never
    calls one, so a placeholder key is enough."""
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setitem(app.config, "OPENROUTER_API_KEY", "test-placeholder")


def test_ask_form_controls_have_names(auth_client, link_id, ai_configured):
    """The send button was an icon with no name -- announced as "button" --
    and the question box had only a placeholder, which disappears on the
    first keystroke. Found once Ask mode entered the UI audit."""
    client, _ = auth_client
    html = client.get(f"/papers/{link_id}?mode=chat").data.decode("utf-8")
    form = re.search(r'<form class="chat-input-area".*?</form>', html, flags=re.S)
    assert form, "Ask mode renders its question form when a model is configured"
    form = form.group(0)

    textarea = re.search(r"<textarea[^>]*>", form, flags=re.S).group(0)
    field_id = re.search(r'id="([^"]+)"', textarea)
    assert field_id, "the question box needs an id for its label to point at"
    assert re.search(rf'<label[^>]*for="{field_id.group(1)}"[^>]*>[^<\s][^<]*</label>', form)

    button = re.search(r'<button type="submit".*?</button>', form, flags=re.S).group(0)
    assert re.search(r'aria-label="[^"]+"', button), "an icon-only send button needs a name"


def test_graph_reset_button_has_a_name(auth_client, link_id):
    client, _ = auth_client
    html = client.get(f"/papers/{link_id}?mode=graph").data.decode("utf-8")
    button = re.search(r'<button[^>]*id="cg-fit-btn"[^>]*>', html, flags=re.S)
    assert button and re.search(r'aria-label="[^"]+"', button.group(0))


def test_ui_audit_renders_every_detail_mode():
    """Each mode draws different markup, and a mode outside the audit is an
    unaudited page -- that is how the Ask form's nameless send button
    survived while the default mode was audited (#138)."""
    source = (Path(__file__).resolve().parents[2] / "scripts" / "render_pages.py").read_text(
        encoding="utf-8"
    )
    for mode in ("tr", "ai", "chat", "graph"):
        assert f"?mode={mode}" in source, f"mode={mode} is not rendered for the UI audit"
