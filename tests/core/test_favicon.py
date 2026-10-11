"""`/favicon.ico` answers, to anyone, with a real icon.

Browsers request it whatever the `<link rel="icon">` tags say; it used to be a
404 on every first visit.
"""

from __future__ import annotations

import io

from PIL import Image


def test_favicon_is_served_anonymously(client):
    r = client.get("/favicon.ico")

    assert r.status_code == 200
    assert r.mimetype == "image/vnd.microsoft.icon"
    assert "max-age=604800" in r.headers["Cache-Control"]


def test_favicon_is_a_multi_size_icon(client):
    """Derived by scripts/render_favicon.py, so it is the mark, not a stub."""
    icon = Image.open(io.BytesIO(client.get("/favicon.ico").data))

    assert icon.format == "ICO"
    assert {(16, 16), (32, 32), (48, 48)} <= set(icon.info["sizes"])


def test_favicon_is_not_a_menu_or_permission_concern(app):
    """No login, no permission: the login page itself asks for it."""
    view = app.view_functions["favicon.favicon"]
    assert not hasattr(view, "__wrapped__")
