"""Registering the OAuth clients — the step that was missing entirely.

`GoogleOAuthStrategy.register()` and its Microsoft twin were the only callers
of `oauth.register(...)`, and nothing called *them*. authlib therefore held no
clients, every OAuth login ended at "Unknown OAuth provider", and the login
page offered both buttons anyway.

The tests here pin both halves of the fix: a provider is registered only when
its credentials are present, and the button is offered only when the provider
is registered. Those have to stay the same condition — a button that cannot
let anyone in is worse than no button.
"""

from __future__ import annotations

import pytest
from flask import Flask

from app import _register_oauth_providers


def _app_with(**config) -> Flask:
    """A bare app carrying only the config this function reads.

    Not `create_app()`: registration depends on four config values and nothing
    else, and a full application would bring a database into a question that
    does not involve one.
    """
    app = Flask(__name__)
    app.config.update(config)

    from app.extensions import oauth

    oauth.init_app(app)
    return app


@pytest.fixture(autouse=True)
def _clean_registry():
    """authlib's OAuth object is a module-level singleton shared by every test."""
    from app.extensions import oauth

    before = dict(getattr(oauth, "_registry", {}))
    before_clients = dict(getattr(oauth, "_clients", {}))
    yield
    if hasattr(oauth, "_registry"):
        oauth._registry.clear()
        oauth._registry.update(before)
    if hasattr(oauth, "_clients"):
        oauth._clients.clear()
        oauth._clients.update(before_clients)


def test_nothing_is_registered_without_credentials():
    """The default state, and it must stay quiet rather than half-working."""
    app = _app_with(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="")

    assert _register_oauth_providers(app) == []
    assert app.config["OAUTH_PROVIDERS"] == []


def test_a_client_id_without_a_secret_is_not_enough():
    """Half a credential is not a credential — the same rule the Zotero
    settings apply. Registering on a partial config would put the button back
    on the page and send people to a provider that rejects them."""
    app = _app_with(GOOGLE_CLIENT_ID="id-only", GOOGLE_CLIENT_SECRET="")

    assert _register_oauth_providers(app) == []


def test_a_configured_provider_is_registered():
    app = _app_with(GOOGLE_CLIENT_ID="an-id", GOOGLE_CLIENT_SECRET="a-secret")

    registered = _register_oauth_providers(app)

    assert registered == ["google"]

    from app.extensions import oauth

    assert getattr(oauth, "google", None) is not None, "the route does this exact lookup"


def test_providers_are_independent():
    """Configuring one must not offer the other."""
    app = _app_with(
        GOOGLE_CLIENT_ID="",
        GOOGLE_CLIENT_SECRET="",
        MICROSOFT_CLIENT_ID="an-id",
        MICROSOFT_CLIENT_SECRET="a-secret",
    )

    assert _register_oauth_providers(app) == ["microsoft"]


def test_the_login_page_offers_no_buttons_when_none_are_configured(client):
    """The visible half of the bug: both buttons were shown unconditionally,
    and both led straight back here with an error."""
    body = client.get("/auth/login").get_data(as_text=True)

    assert "/auth/oauth/google" not in body
    assert "/auth/oauth/microsoft" not in body


def test_the_button_appears_once_the_provider_is_registered(monkeypatch):
    """Same condition as registration, asserted from the page's side.

    A whole application rather than a stubbed variable, because the thing worth
    proving is that configuring credentials is *sufficient* — that the
    registration, the context processor and the template agree. Stubbing the
    variable would test the template against my own assumption about what fills
    it, which is the assumption that was wrong in the first place.
    """
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "an-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "a-secret")
    monkeypatch.delenv("MICROSOFT_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_CLIENT_SECRET", raising=False)

    import importlib

    from app import config as config_module

    importlib.reload(config_module)
    try:
        from app import create_app

        configured = create_app()
        configured.config["WTF_CSRF_ENABLED"] = False
        body = configured.test_client().get("/auth/login").get_data(as_text=True)
    finally:
        importlib.reload(config_module)

    assert "/auth/oauth/google" in body
    assert "/auth/oauth/microsoft" not in body
