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
    babel.locale_selector_func = select_locale
