"""Security headers, and the one condition attached to HSTS.

The app served none of these. The one that matters most here is
`Referrer-Policy`: every paper card links out to a publisher, and this app's
URLs carry the reader's own search terms.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def headers(client):
    return client.get("/auth/login").headers


def test_the_baseline_headers_are_present(headers):
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Permissions-Policy"].startswith("geolocation=()")


def test_the_referrer_policy_does_not_leak_search_terms(headers):
    """`/library/search?q=...` must not travel to a publisher with the click."""
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_hsts_is_absent_without_tls(headers):
    """Dev and test run over plain HTTP.

    Sending HSTS there pins the browser to HTTPS for that host for a year, and
    `localhost` is shared with every other project on the machine — breakage
    that outlives the session that caused it.
    """
    assert "Strict-Transport-Security" not in headers


def test_hsts_appears_once_cookies_are_secure(app, client, monkeypatch):
    """`SESSION_COOKIE_SECURE` is this app's way of saying "we are behind TLS"."""
    monkeypatch.setitem(app.config, "SESSION_COOKIE_SECURE", True)

    sent = client.get("/auth/login").headers["Strict-Transport-Security"]

    assert "max-age=31536000" in sent
    assert "includeSubDomains" in sent


def test_an_existing_header_is_not_overwritten():
    """`setdefault`, so a deployment that hardens nginx too does not end up
    with two conflicting policies.

    A bare app rather than the fixture: this needs a second `after_request`
    registered around ours, and the shared fixture app has already served a
    request by the time a test could add one.

    Flask runs `after_request` in reverse registration order, so the handler
    registered *after* ours runs *before* it — which is exactly the proxy's
    position in the chain.
    """
    from flask import Flask

    from app import _register_security_headers

    bare = Flask(__name__)
    _register_security_headers(bare)

    @bare.after_request
    def _proxy_already_set_it(response):
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @bare.route("/x")
    def x():
        return "ok"

    headers = bare.test_client().get("/x").headers

    assert headers["X-Frame-Options"] == "SAMEORIGIN", "ours must not overwrite"
    assert headers["X-Content-Type-Options"] == "nosniff", "the rest still applied"


# --------------------------------------------------------------------------
# Content-Security-Policy
# --------------------------------------------------------------------------


def test_the_policy_carries_the_directives_that_cost_nothing(headers):
    policy = headers["Content-Security-Policy"]

    assert "base-uri 'self'" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'self'" in policy


def test_script_and_style_src_are_report_only_by_default(headers):
    """Nothing is blocked yet; browsers report what would have been."""
    enforced = headers["Content-Security-Policy"]
    trial = headers["Content-Security-Policy-Report-Only"]

    assert "script-src" not in enforced
    assert "style-src" not in enforced
    assert "script-src 'self' " in trial
    assert "style-src 'self' " in trial
    assert trial.endswith("report-uri /csp-report")


@pytest.mark.parametrize(
    ("flag", "moved", "stays"),
    [
        ("CSP_ENFORCE_SCRIPT_SRC", "script-src", "style-src"),
        ("CSP_ENFORCE_STYLE_SRC", "style-src", "script-src"),
    ],
)
def test_each_directive_is_enforced_on_its_own_flag(app, client, monkeypatch, flag, moved, stays):
    monkeypatch.setitem(app.config, flag, True)

    headers = client.get("/auth/login").headers
    enforced = headers["Content-Security-Policy"]
    trial = headers["Content-Security-Policy-Report-Only"]

    assert f"{moved} 'self' " in enforced and moved not in trial
    assert stays in trial and stays not in enforced
    assert "base-uri 'self'" in enforced, "baseline kept"
    assert "report-uri /csp-report" in enforced, "enforced violations are reported too"


def test_enforcing_both_drops_the_report_only_header(app, client, monkeypatch):
    monkeypatch.setitem(app.config, "CSP_ENFORCE_SCRIPT_SRC", True)
    monkeypatch.setitem(app.config, "CSP_ENFORCE_STYLE_SRC", True)

    headers = client.get("/auth/login").headers

    assert "script-src" in headers["Content-Security-Policy"]
    assert "style-src" in headers["Content-Security-Policy"]
    assert "Content-Security-Policy-Report-Only" not in headers


def test_no_unsafe_allowance_anywhere(headers):
    """No inline script, handlers, `hx-on` or style attributes remain
    (test_csp_readiness.py), so neither escape hatch is needed."""
    for name in ("Content-Security-Policy", "Content-Security-Policy-Report-Only"):
        assert "unsafe-inline" not in headers[name]
        assert "unsafe-eval" not in headers[name]


@pytest.mark.parametrize("name", ["SCRIPT_SRC", "STYLE_SRC"])
def test_cdn_sources_are_pinned_to_a_package(name):
    """A bare jsDelivr host would allow every package on npm."""
    import app as app_module

    sources = getattr(app_module, name).split()[1:]
    cdn = [src for src in sources if "jsdelivr" in src]

    assert cdn, "base.html loads Bootstrap from jsDelivr"
    assert all(src.startswith("https://cdn.jsdelivr.net/npm/") and "@" in src for src in cdn)
    assert all(src.endswith("/") for src in cdn), "a path without / matches one file only"


def _cdn_urls(patterns: list[str]) -> set[str]:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app"
    urls: set[str] = set()
    for path in [*root.rglob("*.html"), *root.rglob("*.js"), *root.rglob("*.css")]:
        if path.name.endswith(".min.js") or "email" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            urls.update(re.findall(pattern, text))
    return urls


def _missing(urls: set[str], directive: str) -> list[str]:
    allowed = [src for src in directive.split()[1:] if src.startswith("https://")]
    return [u for u in sorted(urls) if not any(u.startswith(a) for a in allowed)]


def test_every_cdn_script_in_the_code_is_allowed():
    """A new CDN <script> that the policy does not list would break once enforced."""
    from app import SCRIPT_SRC

    urls = _cdn_urls([r"<script[^>]*src=\"(https://[^\"]+)\"", r"['\"](https://[^'\"]+\.js)['\"]"])

    assert urls, "the scan found nothing -- the patterns no longer match the code"
    assert not _missing(urls, SCRIPT_SRC), "scripts loaded but not in SCRIPT_SRC"


def test_every_cdn_stylesheet_in_the_code_is_allowed():
    from app import STYLE_SRC

    urls = _cdn_urls(
        [
            r"<link[^>]*rel=\"stylesheet\"[^>]*href=\"(https://[^\"]+)\"",
            r"['\"](https://[^'\"]+\.css)['\"]",
            # theme.css pulls its typeface in with @import -- the first run of
            # this policy caught it; the scan had not.
            r"@import\s+url\(['\"]?(https://[^'\")?]+)",
        ]
    )

    assert len(urls) >= 4, "Bootstrap, its icons, vis-network and the Plex @import at least"
    assert not _missing(
        urls, STYLE_SRC
    ), f"stylesheets loaded but not in STYLE_SRC: {_missing(urls, STYLE_SRC)}"


def test_htmx_does_not_inject_its_indicator_style():
    """The <style> htmx adds on load would be blocked by style-src."""
    from pathlib import Path

    base = (Path(__file__).resolve().parents[2] / "app/core/templates/base.html").read_text(
        encoding="utf-8"
    )
    assert '"includeIndicatorStyles": false' in base


# --------------------------------------------------------------------------
# /csp-report
# --------------------------------------------------------------------------


def test_a_legacy_report_is_logged_without_the_query_string(client):
    from structlog.testing import capture_logs

    body = {
        "csp-report": {
            "document-uri": "https://example.test/library/search?q=private+topic",
            "blocked-uri": "https://evil.example.test/x.js?token=abc",
            "violated-directive": "script-src",
            "line-number": 12,
            "original-policy": "script-src 'self'",
        }
    }
    with capture_logs() as logs:
        response = client.post(
            "/csp-report", json=body, headers={"Content-Type": "application/csp-report"}
        )

    assert response.status_code == 204
    [entry] = [e for e in logs if e["event"] == "csp_violation"]
    assert entry["document"] == "https://example.test/library/search"
    assert entry["blocked"] == "https://evil.example.test/x.js"
    assert entry["directive"] == "script-src"
    assert entry["line"] == 12
    assert "private" not in str(entry), "search terms must not reach the log"
    assert "original-policy" not in str(entry), "only known fields are kept"


def test_a_reporting_api_batch_is_logged(client):
    from structlog.testing import capture_logs

    body = [
        {
            "type": "csp-violation",
            "body": {"blockedURL": "inline", "effectiveDirective": "script-src-elem"},
        },
        {
            "type": "csp-violation",
            "body": {"blockedURL": "eval", "effectiveDirective": "script-src"},
        },
    ]
    with capture_logs() as logs:
        response = client.post("/csp-report", json=body)

    assert response.status_code == 204
    assert [e["blocked"] for e in logs if e["event"] == "csp_violation"] == ["inline", "eval"]


@pytest.mark.parametrize("raw", [b"not json", b"{}", b"[1, 2]", b'{"csp-report": "x"}'])
def test_junk_is_accepted_silently(client, raw):
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        response = client.post("/csp-report", data=raw, content_type="application/json")

    assert response.status_code == 204
    assert not [e for e in logs if e["event"] == "csp_violation"]


def test_an_oversized_body_is_not_parsed(client):
    from structlog.testing import capture_logs

    padding = "a" * 20_000
    with capture_logs() as logs:
        response = client.post("/csp-report", json={"csp-report": {"blocked-uri": padding}})

    assert response.status_code == 204
    assert not [e for e in logs if e["event"] == "csp_violation"]


def test_the_report_endpoint_needs_no_csrf_token(app, client, monkeypatch):
    """Browsers send reports without one; with CSRF on this must still land."""
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)

    response = client.post("/csp-report", json={"csp-report": {"blocked-uri": "inline"}})

    assert response.status_code == 204


def test_frame_ancestors_and_x_frame_options_agree(headers):
    """Both are sent; a browser acting on either must reach the same answer."""
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"


def test_headers_are_on_error_responses_too(client):
    """A 404 is still a page a browser renders."""
    headers = client.get("/definitely-not-a-route").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
