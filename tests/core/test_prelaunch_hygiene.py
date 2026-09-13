"""Pre-launch hygiene that is easy to let slip: docs/PRELAUNCH.md O1, O2, O4.

Each of these was found by hand once. The tests are what stops the next
config variable, upload route or audit query from quietly reopening them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------
# O4 -- every environment variable the app reads is in .env.example
# --------------------------------------------------------------------------

#: Read by the code but deliberately not offered to operators.
UNDOCUMENTED_ON_PURPOSE = {
    # The pre-rename spelling of RATELIMIT_STORAGE_URI, kept as a fallback so
    # an old deployment does not silently drop to memory://. Documenting it
    # would invite new deployments to use the name the library ignores.
    "RATELIMIT_STORAGE_URL",
    # Internal switch for the Celery bootstrap path, not a deployment setting.
    "CELERY_WORKER_BOOTSTRAP",
}

ENV_READ = re.compile(r"os\.(?:getenv|environ\.get)\(\s*[\"']([A-Z0-9_]+)[\"']")


def _env_keys_read() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for path in [*(ROOT / "app").rglob("*.py"), ROOT / "wsgi.py"]:
        if not path.exists():
            continue
        for key in ENV_READ.findall(path.read_text(encoding="utf-8")):
            keys.setdefault(key, set()).add(str(path.relative_to(ROOT)))
    return keys


def test_every_env_var_the_app_reads_is_in_env_example():
    """CLAUDE.md asks for this on every new variable; six had slipped, one of
    them a security setting (PROXY_FIX_HOPS). Commented `# KEY=value` lines
    count -- that is how a variable whose default should usually stand is
    documented."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", example, re.MULTILINE))
    keys = _env_keys_read()

    assert len(keys) > 50, "the scan found too little -- the pattern no longer matches"
    missing = {
        key: sorted(paths)
        for key, paths in keys.items()
        if key not in documented and key not in UNDOCUMENTED_ON_PURPOSE
    }
    assert not missing, f"read by the app but missing from .env.example: {missing}"


def test_the_exemptions_are_still_read():
    """An exemption for a variable nobody reads any more is dead weight."""
    assert UNDOCUMENTED_ON_PURPOSE <= set(_env_keys_read())


# --------------------------------------------------------------------------
# O1 -- request bodies are capped in the app, not only in nginx
# --------------------------------------------------------------------------


def test_a_body_cap_is_configured_and_matches_nginx(app):
    """nginx stops bodies at 3m; the app's own cap is the second line."""
    assert app.config["MAX_CONTENT_LENGTH"] == 3 * 1024 * 1024
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "client_max_body_size 3m" in deployment, "keep the two limits in step"


def test_an_oversized_form_post_gets_a_413_page(app, client, monkeypatch):
    monkeypatch.setitem(app.config, "MAX_CONTENT_LENGTH", 1024)

    response = client.post("/auth/login", data={"username": "x" * 4096, "password": "y"})

    assert response.status_code == 413
    assert ">413<" in response.get_data(as_text=True), "the message, not a blank shell"


def test_an_oversized_htmx_post_gets_a_bare_413_for_the_toast(app, client, monkeypatch):
    """app.js shows a toast for 413; a whole error page swapped into a tab
    would be worse than no answer."""
    monkeypatch.setitem(app.config, "MAX_CONTENT_LENGTH", 1024)

    response = client.post(
        "/auth/login", data={"username": "x" * 4096}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 413
    assert response.get_data() == b""


def test_an_oversized_api_post_gets_json(app, client, monkeypatch):
    monkeypatch.setitem(app.config, "MAX_CONTENT_LENGTH", 1024)

    response = client.post("/api/v1/auth/token", json={"username": "x" * 4096})

    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "payload_too_large"


def test_the_toast_for_413_is_wired():
    base = (ROOT / "app/core/templates/base.html").read_text(encoding="utf-8")
    app_js = (ROOT / "app/core/static/js/app.js").read_text(encoding="utf-8")

    assert "data-msg-too-large=" in base
    assert "status === 413" in app_js and "msg('too-large'" in app_js


# --------------------------------------------------------------------------
# O2 -- the audit page's user filter is indexed
# --------------------------------------------------------------------------


def test_audit_logs_are_indexed_for_the_user_filter():
    """The admin audit page filters by user_id and sorts by created_at DESC;
    `(user_id, created_at)` serves both."""
    from app.core.models.audit import AuditLog

    indexed = {tuple(c.name for c in index.columns) for index in AuditLog.__table__.indexes}

    assert ("user_id", "created_at") in indexed


# --------------------------------------------------------------------------
# Error pages reach signed-out visitors
# --------------------------------------------------------------------------


def test_error_pages_render_their_message_when_signed_out(client):
    """base.html renders `auth_content` for anonymous visitors, and the error
    templates only filled `content` -- a signed-out 404 was a blank page. Found
    while checking the new 413 page."""
    body = client.get("/definitely-not-a-route").get_data(as_text=True)

    assert 'class="display-1' in body and ">404<" in body


def test_every_error_template_fills_both_blocks():
    for path in sorted((ROOT / "app/core/templates/errors").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        assert "{% block content %}" in text, path.name
        assert "{% block auth_content %}" in text, f"{path.name} is blank when signed out"
