"""Credential and admin-opt-in gating for sources (Faz 5.1).

No shipped source is gated yet — the adapters that need keys arrive in 5.2/5.4
— so these tests register a synthetic source in the registry for the duration
of each test. That is deliberate: it pins the *mechanism* independently of
whichever source happens to use it, and it means adding EPO OPS later cannot
silently change what these tests assert.
"""

from __future__ import annotations

import pytest
from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.modules.scrape import service
from app.modules.scrape.sources import (
    AVAILABLE_SOURCES,
    SOURCE_META,
    admin_optin_key,
    credentials_ok,
    enabled_sources,
    source_options,
)

_GATED = "test_gated_source"
_SETTING_KEYS = ("gated_enabled", "never_written_key")


@pytest.fixture(autouse=True)
def clean_gate_state(db):
    """The suite shares one database across the session and does not truncate
    between tests, so a `SystemSettings` opt-in row written by one test would
    otherwise decide the next one's outcome. Clear both sides of the gate.
    """
    from app.core.models.settings import SystemSettings
    from app.modules.scrape.models import UserSource

    yield
    SystemSettings.query.filter(SystemSettings.key.in_(_SETTING_KEYS)).delete(
        synchronize_session=False
    )
    UserSource.query.filter_by(source_name=_GATED).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture
def a_user(db):
    user = User.query.filter_by(email="gateduser@example.test").first()
    if user:
        return user
    user = User(
        username="gateduser",
        email="gateduser@example.test",
        full_name="Gated User",
        password_hash=generate_password_hash("password123"),
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def gated_source(monkeypatch):
    """Register `test_gated_source` with a caller-controlled gate.

    Returns a function that (re)writes the source's SOURCE_META with the given
    gating keys. Both dicts are monkeypatched by item so the real registry is
    restored even when a test fails.
    """
    monkeypatch.setitem(AVAILABLE_SOURCES, _GATED, object())

    def configure(**meta):
        monkeypatch.setitem(
            SOURCE_META,
            _GATED,
            {
                "label": "Gated",
                "icon": "bi-lock",
                "desc": "Synthetic source used by the gating tests",
                "url": "https://example.test",
                "topics": ["general"],
                "category": "academic",
                **meta,
            },
        )
        # SCRAPE_SOURCES bypasses BaseConfig (plain os.getenv), so the env var
        # is the only way to get the synthetic source into `_DEFAULT`'s place.
        monkeypatch.setenv("SCRAPE_SOURCES", f"arxiv,{_GATED}")

    return configure


# ----------------------------------------------------------------------------
# credentials_ok / enabled_sources
# ----------------------------------------------------------------------------


def test_ungated_sources_are_always_credentialed():
    """Every Faz 0-4 source declares no `requires_key` — "no gate" must read as
    open, not as closed."""
    for name in ("arxiv", "pubmed", "openalex", "crossref", "web_reach"):
        assert credentials_ok(name) is True


def test_source_without_credentials_is_not_listed(gated_source):
    gated_source(requires_key=True, credentials_ok=lambda: False)
    assert _GATED not in enabled_sources()
    assert _GATED not in {o["name"] for o in source_options()}


def test_source_with_credentials_is_listed(gated_source):
    gated_source(requires_key=True, credentials_ok=lambda: True)
    assert _GATED in enabled_sources()


def test_requires_key_without_a_probe_is_treated_as_missing(gated_source):
    """A source that declares it needs a key but ships no way to check for one
    cannot be assumed configured."""
    gated_source(requires_key=True)
    assert credentials_ok(_GATED) is False
    assert _GATED not in enabled_sources()


def test_raising_probe_counts_as_missing_credentials(gated_source):
    def boom():
        raise RuntimeError("env read blew up")

    gated_source(requires_key=True, credentials_ok=boom)
    assert credentials_ok(_GATED) is False
    assert _GATED not in enabled_sources()


def test_probe_is_evaluated_per_call_not_cached(gated_source):
    """Adding a key must take effect without a restart — the whole reason
    `credentials_ok` is a callable rather than a bool."""
    present = {"key": False}
    gated_source(requires_key=True, credentials_ok=lambda: present["key"])

    assert _GATED not in enabled_sources()
    present["key"] = True
    assert _GATED in enabled_sources()


def test_admin_optin_source_stays_listed_without_the_optin(gated_source):
    """The admin gate is not the credential gate: an admin has to be able to
    see a source in order to decide to turn it on."""
    gated_source(
        requires_key=True,
        credentials_ok=lambda: True,
        requires_admin_optin="gated_enabled",
    )
    assert _GATED in enabled_sources()
    assert admin_optin_key(_GATED) == "gated_enabled"


def test_admin_optin_key_is_none_for_ungated_sources():
    assert admin_optin_key("arxiv") is None


# ----------------------------------------------------------------------------
# effective_source_prefs — the fourth resolution rule
# ----------------------------------------------------------------------------


def test_optin_source_is_off_by_default_once_admin_opted_in(app, db, a_user, gated_source):
    gated_source(
        requires_key=True,
        credentials_ok=lambda: True,
        requires_admin_optin="gated_enabled",
    )
    from app.core.settings.service import set_system_setting

    set_system_setting("gated_enabled", True)

    prefs = service.effective_source_prefs(a_user)
    assert prefs[_GATED] is False
    # arXiv is unaffected — the new rule must not touch ungated sources.
    assert prefs["arxiv"] is True


def test_optin_source_can_be_toggled_on_once_admin_opted_in(app, db, a_user, gated_source):
    gated_source(
        requires_key=True,
        credentials_ok=lambda: True,
        requires_admin_optin="gated_enabled",
    )
    from app.core.settings.service import set_system_setting

    set_system_setting("gated_enabled", True)
    service.set_user_source(a_user, _GATED, True)

    assert service.effective_source_prefs(a_user)[_GATED] is True


def test_admin_optin_off_overrides_an_explicit_user_row(app, db, a_user, gated_source):
    """A stale `UserSource` row must not keep scraping a source the deployment
    has turned off — the licensing/cost decision outranks user preference."""
    gated_source(
        requires_key=True,
        credentials_ok=lambda: True,
        requires_admin_optin="gated_enabled",
    )
    service.set_user_source(a_user, _GATED, True)

    prefs = service.effective_source_prefs(a_user)
    assert prefs[_GATED] is False
    assert _GATED not in service.user_enabled_sources(a_user)


def test_missing_setting_row_reads_as_not_opted_in(app, db, a_user, gated_source):
    gated_source(
        requires_key=True,
        credentials_ok=lambda: True,
        requires_admin_optin="never_written_key",
    )
    assert service.effective_source_prefs(a_user)[_GATED] is False
