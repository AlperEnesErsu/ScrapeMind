"""Settings tab registry.

Tabs live on one of two pages, and the split is the point (issue #58):

* **account** -- `/settings/profile`: who you are and how you sign in. Core
  owns these (`CORE_TABS`).
* **workspace** -- `/settings/workspace`: what the product should do for you.
  Modules register these; in ScrapeMind that is identifiers, interests, AI
  keys, followed authors, saved searches and Zotero.

They used to share one page of fourteen tabs, where a user's LLM provider sat
next to their password. The registry already knew which tabs came from core
and which from modules; only the page merged them.

Usage (in a module's __init__.py or routes.py):

    from app.core.settings.tab_registry import register_profile_tab, set_workspace_title

    register_profile_tab("identifiers", "bi-person-vcard", "Academic Identifiers", _identifiers_ctx)
    set_workspace_title("Research Settings")

A module tab that really is about the account can still join that page with
`page=ACCOUNT_PAGE`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

ACCOUNT_PAGE = "account"
WORKSPACE_PAGE = "workspace"

# --------------------------------------------------------------------------- #
# Core tabs — always present, order matters, always on the account page
# --------------------------------------------------------------------------- #
CORE_TABS: list[tuple[str, str, str]] = [
    ("personal", "bi-person", "Personal Info"),
    ("email", "bi-envelope", "Login Email"),
    ("password", "bi-key", "Password"),
    ("security", "bi-shield-lock", "Security (2FA)"),
    ("prefs", "bi-sliders", "Preferences"),
    ("oauth", "bi-link-45deg", "OAuth Accounts"),
    ("sessions", "bi-laptop", "Active Sessions"),
    ("account", "bi-info-circle", "Account"),
]


# --------------------------------------------------------------------------- #
# Extra tabs registered by modules
# --------------------------------------------------------------------------- #
@dataclass
class ExtraTab:
    code: str
    icon: str
    label_key: str  # plain string or i18n key — rendered by the template
    ctx_builder: Callable  # () -> dict  (called on GET and on partial refreshes)
    page: str = WORKSPACE_PAGE


_EXTRA_TABS: dict[str, ExtraTab] = {}

#: The workspace page's heading, an i18n msgid. Generic in core; a project
#: names it for what its settings are about.
_workspace_title = "Workspace Settings"


def register_profile_tab(
    code: str,
    icon: str,
    label_key: str,
    ctx_builder: Callable,
    *,
    page: str = WORKSPACE_PAGE,
) -> None:
    """Register a module-specific settings tab. Idempotent.

    Module tabs default to the workspace page: a module adds product
    behaviour, not a way to sign in.
    """
    if page not in (ACCOUNT_PAGE, WORKSPACE_PAGE):
        raise ValueError(f"unknown settings page {page!r}")
    _EXTRA_TABS[code] = ExtraTab(
        code=code, icon=icon, label_key=label_key, ctx_builder=ctx_builder, page=page
    )


def set_workspace_title(msgid: str) -> None:
    """Name the workspace page. Pass an English msgid that is in the catalog."""
    global _workspace_title
    _workspace_title = msgid


def workspace_title() -> str:
    return _workspace_title


def tabs_for(page: str) -> list[tuple[str, str, str]]:
    """(code, icon, label) for one page, in display order."""
    result = list(CORE_TABS) if page == ACCOUNT_PAGE else []
    for tab in _EXTRA_TABS.values():
        if tab.page == page:
            result.append((tab.code, tab.icon, tab.label_key))
    return result


def page_of(code: str) -> str | None:
    """The page a tab belongs to, or None if nothing registered it."""
    if code in {t[0] for t in CORE_TABS}:
        return ACCOUNT_PAGE
    tab = _EXTRA_TABS.get(code)
    return tab.page if tab else None


def all_tabs() -> list[tuple[str, str, str]]:
    """Every tab on both pages: (code, icon, label)."""
    return tabs_for(ACCOUNT_PAGE) + tabs_for(WORKSPACE_PAGE)


def is_registered(code: str) -> bool:
    return page_of(code) is not None


def get_extra_ctx_builder(code: str) -> Callable | None:
    tab = _EXTRA_TABS.get(code)
    return tab.ctx_builder if tab else None
