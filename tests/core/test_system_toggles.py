"""Module-registered system settings toggles (Faz 5.1).

The registry exists so `app/core/` can render deployment switches whose meaning
lives in `app/modules/` without importing from there (CLAUDE.md rule 1), so
these tests exercise it with their own synthetic toggle rather than relying on
the two scrape registers.
"""

from __future__ import annotations

import pytest

from app.core.settings import toggle_registry
from app.core.settings.toggle_registry import (
    SystemToggle,
    all_system_toggles,
    register_system_toggle,
    toggle_credentials_ok,
)

_KEY = "test_toggle_enabled"


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    """Work on a copy of the process-wide registry — a test that registered a
    toggle must not leave it on the real settings page for the rest of the
    session."""
    monkeypatch.setattr(toggle_registry, "_TOGGLES", dict(toggle_registry._TOGGLES))


@pytest.fixture(autouse=True)
def clean_setting_row(db):
    yield
    from app.core.models.settings import SystemSettings

    SystemSettings.query.filter_by(key=_KEY).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture
def admin_client(client, db):
    """Logged-in superuser. Same shape as `tests/core/test_system_settings.py`'s
    fixture — the settings POST stamps `updated_by` on every row it writes, so
    a leftover admin from an earlier run can be referenced by several rows and
    has to be unwired before the user can go."""
    from sqlalchemy import text

    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.user import User

    db.session.query(User).filter_by(username="toggleadmin").delete()
    db.session.commit()
    user = User(
        username="toggleadmin",
        email="toggleadmin@example.test",
        full_name="Toggle Admin",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
        is_superuser=True,
    )
    db.session.add(user)
    db.session.commit()
    uid = user.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True

    yield client

    db.session.rollback()
    db.session.execute(text("DELETE FROM system_settings WHERE updated_by = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------


def test_scrape_registers_its_source_toggles():
    """The two Faz 5.1 switches are registered at import time by
    `scrape.routes._register_system_toggles`."""
    keys = {t.key for t in all_system_toggles()}
    assert {"patents_enabled", "scopus_enabled"} <= keys


def test_registration_is_idempotent():
    register_system_toggle(_KEY, label="First")
    register_system_toggle(_KEY, label="Second")

    matching = [t for t in all_system_toggles() if t.key == _KEY]
    assert len(matching) == 1
    assert matching[0].label == "Second"


def test_toggle_without_a_probe_needs_no_credentials():
    assert toggle_credentials_ok(SystemToggle(key=_KEY, label="X")) is True


def test_probe_result_is_reported():
    assert toggle_credentials_ok(SystemToggle(key=_KEY, label="X", credentials_ok=lambda: True))
    assert not toggle_credentials_ok(
        SystemToggle(key=_KEY, label="X", credentials_ok=lambda: False)
    )


def test_raising_probe_reads_as_missing_credentials():
    def boom():
        raise RuntimeError("env unreadable")

    assert toggle_credentials_ok(SystemToggle(key=_KEY, label="X", credentials_ok=boom)) is False


# ----------------------------------------------------------------------------
# The settings page
# ----------------------------------------------------------------------------


def test_get_renders_registered_toggles(admin_client):
    register_system_toggle(_KEY, label="Synthetic toggle", credentials_ok=lambda: True)

    resp = admin_client.get("/settings/system")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Synthetic toggle" in body
    assert _KEY in body


def test_missing_credentials_warning_is_shown(admin_client):
    register_system_toggle(
        _KEY,
        label="Synthetic toggle",
        credentials_ok=lambda: False,
        missing_credentials_hint="Set SYNTHETIC_KEY.",
    )

    body = admin_client.get("/settings/system").get_data(as_text=True)
    assert "Set SYNTHETIC_KEY." in body


def test_configured_toggle_shows_no_warning(admin_client):
    register_system_toggle(
        _KEY,
        label="Synthetic toggle",
        credentials_ok=lambda: True,
        missing_credentials_hint="Set SYNTHETIC_KEY.",
    )

    body = admin_client.get("/settings/system").get_data(as_text=True)
    assert "Set SYNTHETIC_KEY." not in body


def test_post_persists_a_toggle(admin_client, db):
    from app.core.settings.service import get_system_setting

    register_system_toggle(_KEY, label="Synthetic toggle")

    resp = admin_client.post(
        "/settings/system",
        data={
            "app_name": "ScrapeMind",
            "default_locale": "tr",
            "max_user_channels": "10",
            _KEY: "y",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert get_system_setting(_KEY) is True


def test_unchecked_toggle_persists_as_false(admin_client, db):
    """An unchecked checkbox sends nothing at all, so "absent" has to be
    written as False rather than left at whatever it was."""
    from app.core.settings.service import get_system_setting, set_system_setting

    register_system_toggle(_KEY, label="Synthetic toggle")
    set_system_setting(_KEY, True)

    admin_client.post(
        "/settings/system",
        data={"app_name": "ScrapeMind", "default_locale": "tr", "max_user_channels": "10"},
        follow_redirects=True,
    )
    assert get_system_setting(_KEY) is False
