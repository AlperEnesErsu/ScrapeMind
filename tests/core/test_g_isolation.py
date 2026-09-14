"""`g` must not outlive a request or a test (PRELAUNCH O12).

`tests/conftest.py` keeps one application context open for the whole session.
Flask reuses an already-pushed app context for a request instead of pushing a
fresh one, so without a guard `g` was shared by every request in the run.

The visible failure: Flask-Login caches the loaded user on `g._login_user`. In
a test that makes requests as two users, the second user's requests ran as the
first -- which produced a false "200" for a user without permission during
Faz 8 verification, and made the app look broken when it was not.
"""

from __future__ import annotations

import uuid

from flask import g

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User


def _user(db, superuser: bool) -> int:
    suffix = uuid.uuid4().hex[:8]
    u = User(
        username=f"giso-{suffix}",
        email=f"giso-{suffix}@example.test",
        full_name="g isolation",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
        is_superuser=superuser,
    )
    db.session.add(u)
    db.session.commit()
    return u.id


def _client_as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def test_a_second_user_is_not_served_the_first_users_identity(app, db):
    admin_id, plain_id = _user(db, True), _user(db, False)
    admin, plain = _client_as(app, admin_id), _client_as(app, plain_id)

    assert admin.get("/admin/menu/").status_code == 200
    # The same shared context: before the fix this returned 200 as the admin.
    assert plain.get("/admin/menu/").status_code == 403


def test_g_written_by_a_test_does_not_log_a_request_in(app, db):
    """A user object left on `g` by test code must not authenticate a later
    anonymous request -- in production every request starts with a fresh `g`."""
    admin = db.session.get(User, _user(db, True))
    g._login_user = admin
    anonymous = app.test_client()
    assert anonymous.get("/admin/menu/").status_code in (301, 302)


def test_g_is_empty_at_the_start_of_a_test_part_one(app):
    g.o12_marker = "set by part one"


def test_g_is_empty_at_the_start_of_a_test_part_two(app):
    """Runs after part one in file order; under random ordering it is simply
    vacuous rather than flaky."""
    assert getattr(g, "o12_marker", None) is None
