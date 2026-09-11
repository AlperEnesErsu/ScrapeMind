"""The one-shot flag behind the login animation.

Set when a login completes, consumed by the first full page that renders
afterwards, and gone after that. A splash the user sits through on every
navigation is not a welcome, it is a toll.

Why a session flag and not "animate on the dashboard": there are three ways
into this app (password, 2FA challenge, OAuth callback) and `next=` can send
any of them somewhere other than the dashboard. The flag rides the session, so
whichever page lands first is the one that plays it.

Why popped from the template rather than `before_request`: HTMX fragments and
static files are requests too, and popping there would spend the flag on a
notification poll the user never sees. Only `base.html` calls this, and HTMX
partials do not extend it.
"""

from __future__ import annotations

from flask import session

SPLASH_KEY = "_splash"


def arm_splash() -> None:
    """Play the animation on the next full page render."""
    session[SPLASH_KEY] = True


def pop_splash() -> bool:
    """True once, for the render that follows a login."""
    return bool(session.pop(SPLASH_KEY, False))
