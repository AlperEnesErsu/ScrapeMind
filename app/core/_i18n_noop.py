"""i18n extraction anchor — never imported at runtime.

Some msgids are passed to `_()` only via dynamic data (menu_items.label_key,
tab_registry CORE_TABS, audit action constants). Pybabel's static
extraction can't see those calls and would mark the catalog entries
obsolete on every `pybabel update` pass.

Listing the keys here with `lazy_gettext` keeps them in the catalog so the
translator workflow stays usable. Add a key here whenever you introduce a
new DB-driven label.
"""

from flask_babel import lazy_gettext as _l

# Sidebar menu (synced with menu_items seed migration f3b1a0c2d8e7)
_MENU_LABELS = [
    _l("menu.dashboard"),
    _l("menu.discover"),
    _l("menu.library"),
    _l("menu.library.timeline"),
    _l("menu.library.favorites"),
    _l("menu.library.notes"),
]

# Profile tabs (synced with app/core/settings/tab_registry.py CORE_TABS)
_TAB_LABELS = [
    _l("Personal Info"),
    _l("Login Email"),
    _l("Password"),
    _l("Security (2FA)"),
    _l("Preferences"),
    _l("OAuth Accounts"),
    _l("Active Sessions"),
    _l("Account"),
]
