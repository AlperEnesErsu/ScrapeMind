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
# Content-Security-Policy — the half that can ship today
# --------------------------------------------------------------------------


def test_the_policy_carries_the_directives_that_cost_nothing(headers):
    policy = headers["Content-Security-Policy"]

    assert "base-uri 'self'" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'self'" in policy


def test_the_policy_does_not_pretend_to_cover_scripts(headers):
    """A `script-src` with `'unsafe-inline'` is a policy in name only.

    Eight templates carry inline `<script>` and thirty-six inline event
    handlers sit across eighteen more — CSP blocks both alike. Until those are
    gone the honest thing is to omit the directive rather than neuter it, so
    nobody reads the header and believes scripts are constrained.
    """
    policy = headers["Content-Security-Policy"]

    assert "script-src" not in policy
    assert "unsafe-inline" not in policy, "an unsafe-inline allowance must not creep in"


def test_frame_ancestors_and_x_frame_options_agree(headers):
    """Both are sent; a browser acting on either must reach the same answer."""
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"


def test_headers_are_on_error_responses_too(client):
    """A 404 is still a page a browser renders."""
    headers = client.get("/definitely-not-a-route").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
