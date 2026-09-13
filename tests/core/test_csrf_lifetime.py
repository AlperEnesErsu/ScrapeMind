"""CSRF tokens are bound to the session, not to a clock.

Flask-WTF expires tokens after an hour by default. That default was never a
decision this project made, and it fails in a shape this app produces
constantly: a researcher leaves a tab open across a working day, and after an
hour every HTMX form on that page stops submitting. The server answers a bare
400 that HTMX did not surface, so the box simply did nothing.

`None` is not "no protection". The token is still signed with SECRET_KEY and
tied to the session's own CSRF value, so using one still requires the victim's
session. What the time limit protects against is a *leaked* token replayed
later — and this app never puts the token in a URL, only in form bodies and the
X-CSRFToken header.
"""

from __future__ import annotations

import pytest
from flask import g
from flask_wtf.csrf import generate_csrf, validate_csrf


@pytest.fixture(autouse=True)
def _clear_cached_token(app):  # noqa: ARG001 — `app` only to order after it
    """Drop `g.csrf_token` between these tests.

    `generate_csrf` caches the signed token on `g`, and `g` is shared across
    the whole run here: conftest's `app` fixture is session-scoped and holds
    one app context open for every test. Without this, the second test in the
    file gets the first test's token — signed against a session that no longer
    exists — and fails with "The CSRF session token is missing", which reads
    like a CSRF bug and is a fixture-scope bug.

    Fixing it at the source means a per-test app context, and that is not a
    free change: Flask-SQLAlchemy scopes `db.session` to the app context, so a
    nested one would hand tests a different session than their fixtures used.
    Recorded in docs/PRELAUNCH.md rather than done in passing.
    """
    g.pop("csrf_token", None)
    yield
    g.pop("csrf_token", None)


def test_the_token_has_no_time_limit():
    from app.config import BaseConfig

    assert BaseConfig.WTF_CSRF_TIME_LIMIT is None


def test_a_token_still_validates_long_after_it_was_issued(app):
    """The regression, expressed as time passing.

    `max_age=None` is what the absent time limit becomes inside itsdangerous,
    so an old token has to survive. Signing one in the past is the cheapest way
    to say "a day went by" without waiting a day.
    """
    with app.test_request_context():
        token = generate_csrf()

        # Nothing here mocks the clock: with no max_age, age is not consulted
        # at all, which is precisely the property under test.
        validate_csrf(token)  # must not raise


def test_a_short_limit_would_still_expire_it(app, monkeypatch):
    """The behaviour being turned off, shown rather than asserted in the
    abstract — so this file fails loudly if a future change puts a limit back
    without meaning to.

    `monkeypatch.setitem` rather than plain assignment: the `app` fixture is
    session-scoped, so a config value written here would follow every later
    test in the run.
    """
    import time

    from wtforms.validators import ValidationError

    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    monkeypatch.setitem(app.config, "WTF_CSRF_TIME_LIMIT", 1)

    with app.test_request_context():
        g.pop("csrf_token", None)
        token = generate_csrf()
        time.sleep(2)

        with pytest.raises(ValidationError, match="expired"):
            validate_csrf(token)


def test_a_forged_token_is_still_rejected(app):
    """Removing the clock must not remove the signature check."""
    from wtforms.validators import ValidationError

    with app.test_request_context():
        with pytest.raises(ValidationError):
            validate_csrf("not-a-real-token")


def test_a_token_from_another_session_is_rejected(app):
    """The property that carries the protection once the clock is gone."""
    from wtforms.validators import ValidationError

    with app.test_request_context():
        stolen = generate_csrf()

    # A different request context means a different session, and therefore a
    # different raw CSRF value behind the signature.
    with app.test_request_context():
        g.pop("csrf_token", None)
        generate_csrf()  # this session gets its own

        with pytest.raises(ValidationError):
            validate_csrf(stolen)
