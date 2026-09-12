"""A test must not start inside a request it did not ask for.

`pytest-flask` used to push a request context around every test that touched
the `app` fixture. Nothing in this suite asked for that, and the cost was not
theoretical: it made the suite structurally unable to reproduce "this code
needs a request context and does not have one", which is how the saved-search
alerts shipped dead (docs/HANDOVER.md §5.8) and how a template-backed email
from a task would have shipped dead next.

Removing the plugin fixed thirty-six failures by fixing two places rather than
thirty-six tests — the selector in `app/core/i18n/utils.py` and the menu
context processor — because all thirty-six were the same assumption in two
spots.

This file exists so the blind spot cannot come back quietly. Reinstalling
`pytest-flask`, or anything else that pushes an ambient request, fails here
rather than six months later in production.
"""

from __future__ import annotations

from flask import has_app_context, has_request_context


def test_a_bare_test_has_no_request_context():
    """Nothing at all, with no fixtures involved."""
    assert has_request_context() is False


def test_the_app_fixture_does_not_bring_a_request_with_it(app):
    """The specific thing `pytest-flask` did.

    A test that wants a request pushes one itself — `app.test_request_context()`
    or a `client` call — and then the test says so in its own body, which is
    where that fact belongs.
    """
    assert has_request_context() is False, (
        "something is pushing an ambient request context; the suite cannot see "
        "missing-request-context bugs while that is true"
    )
    assert app is not None


def test_the_db_fixture_does_not_either(db):
    """`db` pulls in `app`, so it would inherit the same problem."""
    assert has_request_context() is False
    assert has_app_context() is True, "an app context is expected; a request is not"


def test_a_test_can_still_ask_for_one(app):
    """The replacement for the ambient context: ask, explicitly, in the test."""
    with app.test_request_context("/?lang=en"):
        assert has_request_context() is True

    assert has_request_context() is False, "and it goes away again"
