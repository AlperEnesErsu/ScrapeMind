"""The login animation's one-shot flag.

The animation itself is CSS and is not testable here. What is testable, and
what actually matters, is that it plays exactly once: an intro the user sits
through on every page load is not a welcome, it is a toll.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.ui.splash import SPLASH_KEY, pop_splash


@pytest.fixture
def credentials(db):
    """A registered user and their password."""
    from app.core.auth.service import register_user

    suffix = uuid.uuid4().hex[:8]
    password = "Splash1234"
    user, error = register_user(
        username=f"splash-{suffix}",
        email=f"splash-{suffix}@example.test",
        full_name="Splash tester",
        password=password,
    )
    assert error is None
    user_id = user.id
    yield user, password

    # A completed login is the only thing that arms the flag, so these tests
    # have to do a real one -- and a real one writes an audit row and a session
    # row that outlive the `db` fixture's rollback, because the login route
    # commits. `tests/core/test_users.py` bulk-deletes every User, which the
    # audit row's foreign key then blocks. Nothing else in this suite completes
    # a login, so this file is the first to owe that cleanup.
    from sqlalchemy import text

    db.session.rollback()
    for table in ("audit_logs", "user_sessions", "user_roles", "user_settings"):
        db.session.execute(text(f"DELETE FROM {table} WHERE user_id = :uid"), {"uid": user_id})
    db.session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
    db.session.commit()


def test_logging_in_arms_the_animation(client, credentials):
    user, password = credentials

    client.post(
        "/auth/login",
        data={"username": user.username, "password": password},
        follow_redirects=False,
    )

    with client.session_transaction() as session:
        assert session.get(SPLASH_KEY) is True


def test_it_plays_once_and_then_never_again(client, credentials):
    """The whole point. A second page load must not replay it."""
    user, password = credentials

    client.post(
        "/auth/login",
        data={"username": user.username, "password": password},
        follow_redirects=False,
    )

    first = client.get("/", follow_redirects=True).get_data(as_text=True)
    second = client.get("/", follow_redirects=True).get_data(as_text=True)

    assert 'class="splash"' in first
    assert 'class="splash"' not in second


def test_a_page_without_a_login_never_shows_it(client, credentials):
    """Nothing arms the flag but a completed login."""
    user, password = credentials

    client.post(
        "/auth/login",
        data={"username": user.username, "password": password},
        follow_redirects=False,
    )
    with client.session_transaction() as session:
        session.pop(SPLASH_KEY, None)

    assert 'class="splash"' not in client.get("/", follow_redirects=True).get_data(as_text=True)


def test_the_overlay_is_hidden_from_assistive_technology(client, credentials):
    """It is decoration with no text. A screen reader should land on the page."""
    user, password = credentials

    client.post(
        "/auth/login",
        data={"username": user.username, "password": password},
        follow_redirects=False,
    )
    html = client.get("/", follow_redirects=True).get_data(as_text=True)

    marker = html.index('class="splash"')
    opening_tag = html[html.rindex("<div", 0, marker) : html.index(">", marker) + 1]
    assert 'aria-hidden="true"' in opening_tag
    assert "inert" in opening_tag


def test_popping_the_flag_clears_it(app):
    """`pop_splash` is a read *and* a clear -- the template calls it once."""
    with app.test_request_context():
        from flask import session

        session[SPLASH_KEY] = True
        assert pop_splash() is True
        assert pop_splash() is False
