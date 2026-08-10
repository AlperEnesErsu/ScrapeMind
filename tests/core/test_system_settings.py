"""Admin system-settings page: `max_user_channels`.

Covers the first numeric value stored through the generic SystemSettings
mechanism (see app/core/settings/service.py). Nothing consumes the value
yet — these tests only check that an admin can set it, that it round-trips
as an int (the column is JSON, so a naive implementation could persist a
string), that out-of-range values are rejected, and that the route stays
admin-only.
"""

import pytest
from sqlalchemy import text

from app.core.models.settings import SystemSettings
from app.core.models.user import User
from app.core.settings.service import get_system_setting


@pytest.fixture
def admin(db):
    from app.core.auth.strategies.local import LocalAuthStrategy

    # The system-settings POST stamps updated_by on every setting it writes
    # (app_name, default_locale, ... not just max_user_channels), so a
    # leftover admin from a prior run can be referenced by several rows.
    db.session.execute(
        text(
            "DELETE FROM system_settings WHERE updated_by IN "
            "(SELECT id FROM users WHERE username = 'settingsadmin')"
        )
    )
    db.session.execute(
        text(
            "DELETE FROM audit_logs WHERE user_id IN "
            "(SELECT id FROM users WHERE username = 'settingsadmin')"
        )
    )
    db.session.query(User).filter_by(username="settingsadmin").delete()
    db.session.commit()
    u = User(
        username="settingsadmin",
        email="settingsadmin@example.test",
        full_name="Settings Admin",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
        is_superuser=True,
    )
    db.session.add(u)
    db.session.commit()
    uid = u.id
    yield u
    db.session.rollback()
    db.session.execute(text("DELETE FROM system_settings WHERE updated_by = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


def _login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True


def _valid_payload(**overrides):
    payload = {
        "app_name": "ScrapeMind",
        "default_locale": "tr",
        "max_user_channels": "25",
    }
    payload.update(overrides)
    return payload


def test_post_persists_max_user_channels_as_int(client, admin, db):
    _login(client, admin.id)
    r = client.post("/settings/system", data=_valid_payload(max_user_channels="25"))
    assert r.status_code == 302

    row = SystemSettings.query.get("max_user_channels")
    assert row is not None
    assert row.value == 25
    assert isinstance(row.value, int)
    assert get_system_setting("max_user_channels") == 25
    assert isinstance(get_system_setting("max_user_channels"), int)


def test_get_form_prefilled_with_config_default_when_no_row(client, admin, app):
    _login(client, admin.id)
    body = client.get("/settings/system").get_data(as_text=True)
    default = app.config.get("MAX_USER_CHANNELS", 10)
    assert f'value="{default}"' in body


@pytest.mark.parametrize("bad_value", ["-1", "500"])
def test_out_of_range_values_rejected(client, admin, bad_value):
    _login(client, admin.id)
    r = client.post("/settings/system", data=_valid_payload(max_user_channels=bad_value))
    # Validation failure re-renders the form (200), it does not redirect.
    assert r.status_code == 200
    assert SystemSettings.query.get("max_user_channels") is None


def test_non_admin_gets_403(auth_client):
    client, _uid = auth_client
    r = client.get("/settings/system")
    assert r.status_code == 403
