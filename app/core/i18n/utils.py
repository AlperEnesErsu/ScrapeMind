from flask import current_app, g, has_request_context, request
from flask_babel import Babel
from flask_login import current_user

SUPPORTED_LOCALES = ["tr", "en"]


def recipient_locale(user) -> str:
    """The language background work should address `user` in.

    Outside a request there is no browser to ask, so a worker that writes
    something a person will read -- a notification, a report's narration --
    wraps that in `force_locale(recipient_locale(user))`: the *recipient's*
    stored preference, not whoever happens to run the worker. A stored locale
    we do not ship (or no user at all) falls back to the configured default,
    the same answer `select_locale` gives when there is no request.
    """
    locale = getattr(user, "locale", None)
    if locale in SUPPORTED_LOCALES:
        return locale
    return current_app.config.get("BABEL_DEFAULT_LOCALE", "tr")


def select_locale() -> str:
    # 0. No request at all — Celery beat, a worker, a CLI command, a script.
    #
    # Every step below reads the request, so without this the selector raised
    # `RuntimeError: Working outside of request context` and every `_()` in
    # background code went with it. That is not hypothetical: it is what made
    # Faz 7.1's saved-search alerts ship dead. The exception was caught by the
    # per-search `except` that exists so one bad search cannot lose the others,
    # logged, and the feature looked from outside exactly like "no new matches"
    # (docs/HANDOVER.md §5.8, PR #71).
    #
    # Raising bought nothing there. It was never seen by a person, because the
    # only code positioned to see it was code written to keep going. The
    # configured default is the honest answer instead: a caller that knows
    # whose language this is wraps the call in `force_locale` -- `digest_tasks`,
    # `report_tasks` and `alerts` all do -- and one that forgets now sends the
    # default language rather than sending nothing.
    #
    # That trade is deliberate and it is not free: a background job that forgets
    # `force_locale` will quietly address an English reader in Turkish. A
    # notification in the wrong language is visible and fixable. A notification
    # that was never created is neither.
    if not has_request_context():
        return current_app.config.get("BABEL_DEFAULT_LOCALE", "tr")

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

    @app.context_processor
    def inject_current_locale() -> dict:
        """`current_locale` for templates, resolved before the template runs.

        Templates used to read `g.locale`, which `select_locale` fills only
        when Flask-Babel first resolves the locale -- lazily, on the first
        `_()`. `<html lang>` is the second line of base.html, rendered before
        any `_()`, so it always printed the fallback: an English page went out
        as `lang="tr"`, and a screen reader read it with Turkish
        pronunciation. Calling `get_locale()` here runs the selector up front.
        """
        from flask_babel import get_locale

        locale = get_locale()
        return {"current_locale": str(locale) if locale else "tr"}
