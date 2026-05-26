"""Profile tab registry.

Core defines the base tabs; modules call register_profile_tab() at
import/startup time to inject project-specific tabs.

Usage (in a module's __init__.py or routes.py):

    from app.core.settings.tab_registry import register_profile_tab
    from app.modules.academic import _identifiers_ctx, _interests_ctx

    register_profile_tab("identifiers", "bi-person-vcard", "Academic Identifiers", _identifiers_ctx)
    register_profile_tab("interests",   "bi-tags",         "Research Interests",   _interests_ctx)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# --------------------------------------------------------------------------- #
# Core tabs — always present, order matters
# --------------------------------------------------------------------------- #
CORE_TABS: list[tuple[str, str, str]] = [
    ("personal", "bi-person",       "Personal Info"),
    ("email",    "bi-envelope",     "Login Email"),
    ("password", "bi-key",          "Password"),
    ("security", "bi-shield-lock",  "Security (2FA)"),
    ("prefs",    "bi-sliders",      "Preferences"),
    ("oauth",    "bi-link-45deg",   "OAuth Accounts"),
    ("sessions", "bi-laptop",       "Active Sessions"),
    ("account",  "bi-info-circle",  "Account"),
]

# --------------------------------------------------------------------------- #
# Extra tabs registered by modules
# --------------------------------------------------------------------------- #
@dataclass
class ExtraTab:
    code: str
    icon: str
    label_key: str          # plain string or i18n key — rendered by the template
    ctx_builder: Callable   # () -> dict  (called on GET and on partial refreshes)


_EXTRA_TABS: dict[str, ExtraTab] = {}


def register_profile_tab(
    code: str,
    icon: str,
    label_key: str,
    ctx_builder: Callable,
) -> None:
    """Register a module-specific profile tab. Idempotent."""
    _EXTRA_TABS[code] = ExtraTab(code=code, icon=icon, label_key=label_key, ctx_builder=ctx_builder)


def all_tabs() -> list[tuple[str, str, str]]:
    """Returns merged list of (code, icon, label) for all tabs."""
    result = list(CORE_TABS)
    for tab in _EXTRA_TABS.values():
        result.append((tab.code, tab.icon, tab.label_key))
    return result


def is_registered(code: str) -> bool:
    core_codes = {t[0] for t in CORE_TABS}
    return code in core_codes or code in _EXTRA_TABS


def get_extra_ctx_builder(code: str) -> Callable | None:
    tab = _EXTRA_TABS.get(code)
    return tab.ctx_builder if tab else None
