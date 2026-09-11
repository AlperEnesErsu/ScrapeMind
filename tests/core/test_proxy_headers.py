"""Reading X-Forwarded-* — and refusing to, unless a proxy is declared.

nginx has always sent these headers (docs/DEPLOYMENT.md §3); the app never
read them. Two things broke silently: the login rate limit stopped being per
client and became one bucket for the whole site, and `_external` URLs — the
links in password-reset emails — came out as `http://`.

The tests below cover both directions, because this setting is dangerous in
both. Trusting nothing when a proxy *is* there collapses the rate limit;
trusting a hop that is *not* there lets a client choose its own address, and
therefore its own rate-limit bucket.
"""

from __future__ import annotations

import pytest
from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from app import _apply_proxy_fix

FORGED = "203.0.113.9"


def _probe_app(hops: int) -> Flask:
    """A bare app carrying only the behaviour under test.

    Deliberately not `create_app()`: this is about one middleware and its one
    config key, and a full application would drag a database and a Celery app
    into a question that involves neither.
    """
    app = Flask(__name__)
    app.config["PROXY_FIX_HOPS"] = hops
    _apply_proxy_fix(app)

    @app.route("/whoami")
    def whoami():
        from flask import request

        return jsonify(ip=request.remote_addr, scheme=request.scheme, secure=request.is_secure)

    return app


@pytest.fixture
def forwarded_headers():
    return {"X-Forwarded-For": FORGED, "X-Forwarded-Proto": "https"}


def test_without_a_declared_proxy_the_headers_are_ignored(forwarded_headers):
    """The safe default, and it has to stay safe.

    With no proxy in front, anyone can send X-Forwarded-For. Honouring it would
    let a client pick its own rate-limit bucket — a worse failure than the one
    this feature fixes.
    """
    client = _probe_app(hops=0).test_client()

    body = client.get("/whoami", headers=forwarded_headers).get_json()

    assert body["ip"] != FORGED
    assert body["scheme"] == "http"


def test_with_one_proxy_the_client_address_comes_through(forwarded_headers):
    client = _probe_app(hops=1).test_client()

    body = client.get("/whoami", headers=forwarded_headers).get_json()

    assert body["ip"] == FORGED, "rate limiting counts this address"


def test_with_one_proxy_the_scheme_comes_through(forwarded_headers):
    """`is_secure` is what `url_for(_external=True)` builds links from."""
    client = _probe_app(hops=1).test_client()

    body = client.get("/whoami", headers=forwarded_headers).get_json()

    assert body["scheme"] == "https"
    assert body["secure"] is True


def test_one_declared_hop_does_not_trust_two(forwarded_headers):
    """A client prepending an address must not be able to choose its bucket.

    With one real proxy, `X-Forwarded-For: <forged>, <real>` should resolve to
    the address the proxy appended, not the one the client invented.
    """
    client = _probe_app(hops=1).test_client()

    body = client.get(
        "/whoami",
        headers={"X-Forwarded-For": f"{FORGED}, 198.51.100.4"},
    ).get_json()

    assert body["ip"] == "198.51.100.4"
    assert body["ip"] != FORGED


def test_production_declares_one_hop():
    """Matching the single nginx in docs/DEPLOYMENT.md §3."""
    from app.config import BaseConfig, ProductionConfig

    assert BaseConfig.PROXY_FIX_HOPS == 0, "off unless deliberately turned on"
    assert ProductionConfig.PROXY_FIX_HOPS == 1


def test_the_middleware_is_actually_installed():
    """`_apply_proxy_fix` wraps `wsgi_app`; a no-op would pass every test above
    that asserts the *absence* of an effect, so assert the presence too."""
    assert isinstance(_probe_app(hops=1).wsgi_app, ProxyFix)
    assert not isinstance(_probe_app(hops=0).wsgi_app, ProxyFix)
