"""`<html lang>` names the language the page is actually in.

base.html read `g.locale`, which the locale selector fills only when
Flask-Babel first resolves the locale -- lazily, on the first `_()`. The
`<html>` tag renders before any `_()`, so every page said `lang="tr"`, English
ones included, and a screen reader read English text with Turkish
pronunciation (WCAG 3.1.1).
"""

import re

import pytest


def _html_lang(response) -> str:
    match = re.search(r'<html lang="([^"]*)"', response.get_data(as_text=True))
    assert match, "no <html lang>"
    return match.group(1)


def _topbar_lang(response) -> str | None:
    # The language button's visible label, upper-cased by the template.
    match = re.search(r">\s*(TR|EN)\s*<", response.get_data(as_text=True))
    return match.group(1) if match else None


@pytest.mark.parametrize(
    "path, headers, expected",
    [
        ("/auth/login?lang=en", {}, "en"),
        ("/auth/login?lang=tr", {"Accept-Language": "en"}, "tr"),
        ("/auth/login", {"Accept-Language": "en-US,en;q=0.9"}, "en"),
        ("/auth/login", {"Accept-Language": "tr-TR"}, "tr"),
        ("/definitely-not-a-page?lang=en", {}, "en"),
    ],
)
def test_html_lang_follows_the_resolved_locale(client, path, headers, expected):
    assert _html_lang(client.get(path, headers=headers)) == expected


def test_html_lang_follows_the_locale_cookie(client):
    # The cookie /settings/set-locale writes.
    client.set_cookie("lang", "en")
    assert _html_lang(client.get("/auth/login", headers={"Accept-Language": "tr"})) == "en"


def test_signed_in_user_preference_reaches_html_lang_and_topbar(auth_client, db):
    from app.core.models.user import User

    client, uid = auth_client
    user = db.session.get(User, uid)
    user.locale = "en"
    db.session.commit()

    response = client.get("/papers/", headers={"Accept-Language": "tr"})
    assert _html_lang(response) == "en"
    assert _topbar_lang(response) in (None, "EN")
