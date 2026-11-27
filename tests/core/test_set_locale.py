"""The language a signed-in user picks follows them, not just their browser.

`/settings/set-locale/<lang>` wrote the choice to `UserSettings.settings
["locale"]`, a key nothing reads. The locale selector, the profile form, the
digest emails and the API all use `users.locale`. So the choice survived only
as long as the `lang` cookie did: a new browser, another device or a cleared
cookie put the user back in Turkish. Found by the e2e smoke suite
(e2e/tests/i18n.spec.mjs), which opens a second browser with the same session.
"""


def test_set_locale_saves_the_choice_to_the_user(auth_client, db):
    from app.core.models.user import User

    client, uid = auth_client
    response = client.get("/settings/set-locale/en")
    assert response.status_code == 302

    db.session.expire_all()
    assert db.session.get(User, uid).locale == "en"


def test_saved_choice_applies_without_the_cookie(auth_client):
    client, _ = auth_client
    client.get("/settings/set-locale/en")
    # A second browser: same session, no language cookie, a Turkish browser.
    client.delete_cookie("lang")

    response = client.get("/papers/", headers={"Accept-Language": "tr"})
    assert '<html lang="en"' in response.get_data(as_text=True)


def test_unsupported_locale_is_rejected_and_changes_nothing(auth_client, db):
    from app.core.models.user import User

    client, uid = auth_client
    assert client.get("/settings/set-locale/xx").status_code == 400

    db.session.expire_all()
    assert db.session.get(User, uid).locale == "tr"
