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
    # 2. Authenticated user's preference
    if current_user.is_authenticated and current_user.locale in SUPPORTED_LOCALES:
        g.locale = current_user.locale
        return current_user.locale
    # 3. Browser Accept-Language
    best = request.accept_languages.best_match(SUPPORTED_LOCALES)
    g.locale = best or "tr"
    return g.locale


def init_babel(app, babel: Babel) -> None:
    babel.locale_selector_func = select_locale
