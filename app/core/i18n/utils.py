from flask import g, request
from flask_babel import Babel
from flask_login import current_user

SUPPORTED_LOCALES = ["tr", "en"]


def select_locale() -> str:
    # 1. URL param override
    lang = request.args.get("lang")
    if lang in SUPPORTED_LOCALES:
        g.locale = lang
        return lang
    # 2. Cookie preference
    cookie_lang = request.cookies.get("lang")
    if cookie_lang in SUPPORTED_LOCALES:
        g.locale = cookie_lang
        return cookie_lang
    # 3. Authenticated user's preference
    if current_user.is_authenticated and current_user.locale in SUPPORTED_LOCALES:
        g.locale = current_user.locale
        return current_user.locale
    # 4. Browser Accept-Language
    best = request.accept_languages.best_match(SUPPORTED_LOCALES)
    g.locale = best or "tr"
    return g.locale


def init_babel(app, babel: Babel) -> None:
    """Wire the selector, and mind which API is being wired.

    ``babel.locale_selector_func = ...`` is the Flask-Babel 2.x form. Under 3.x
    and later it assigns an attribute nothing reads, so the selector never ran:
    every request fell back to BABEL_DEFAULT_LOCALE, ``?lang=en`` did nothing,
    the cookie ``/settings/set-locale`` sets was never read back, and ``g.locale``
    -- which the topbar prints as the language button's only label -- stayed
    unset, leaving that button empty and nameless to a screen reader.
    """
    babel.init_app(app, locale_selector=select_locale)
