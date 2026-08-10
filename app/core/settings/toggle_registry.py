"""System-settings toggle registry.

Same shape and reasoning as `tab_registry.py`: core owns the page, modules
inject the rows. Faz 5.1 needs two deployment-level switches ("may this
install call the patent APIs / Scopus") whose *meaning* belongs entirely to
`app/modules/scrape` — including how to tell whether the corresponding API
keys are configured. Hard-coding them into `SystemSettingsForm` would put
knowledge of EPO OPS and Elsevier inside `app/core/`, which never imports from
`app/modules/` (CLAUDE.md rule 1).

So core stores and renders a list of booleans it knows nothing about, and the
module registers what they mean:

    from app.core.settings.toggle_registry import register_system_toggle

    register_system_toggle(
        "patents_enabled",
        label="Patent sources",
        help_text="Nightly patent scanning via EPO OPS and PatentsView.",
        credentials_ok=lambda: bool(os.getenv("EPO_OPS_KEY")),
        missing_credentials_hint="EPO_OPS_KEY / EPO_OPS_SECRET are not set.",
    )

`credentials_ok` exists so the settings page can say "this switch will do
nothing until you configure a key" *next to the switch*, instead of letting an
admin turn something on and watch nothing happen.

Values are stored in `SystemSettings` under `key`, so anything already able to
read a system setting can read a toggle — no second storage mechanism.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class SystemToggle:
    key: str
    label: str  # English msgid — the template wraps it in `_()`
    help_text: str = ""  # English msgid, may be empty
    #: () -> bool. None means the toggle needs no external credentials.
    credentials_ok: Callable[[], bool] | None = None
    #: English msgid shown when `credentials_ok()` is False.
    missing_credentials_hint: str = ""


_TOGGLES: dict[str, SystemToggle] = {}


def register_system_toggle(
    key: str,
    *,
    label: str,
    help_text: str = "",
    credentials_ok: Callable[[], bool] | None = None,
    missing_credentials_hint: str = "",
) -> None:
    """Register a module-owned boolean on the system settings page. Idempotent
    — re-registering the same key replaces it, so a module import that runs
    twice (test app factories do this) cannot produce duplicate rows."""
    _TOGGLES[key] = SystemToggle(
        key=key,
        label=label,
        help_text=help_text,
        credentials_ok=credentials_ok,
        missing_credentials_hint=missing_credentials_hint,
    )


def all_system_toggles() -> list[SystemToggle]:
    """Registered toggles in registration order."""
    return list(_TOGGLES.values())


def toggle_credentials_ok(toggle: SystemToggle) -> bool:
    """Whether `toggle`'s external credentials are configured.

    True when the toggle declares no probe — "no credentials needed" must not
    render as a warning. A probe that raises counts as *not* configured, the
    same call `sources.credentials_ok` makes: an unreadable probe is not
    evidence that the key is there.
    """
    if toggle.credentials_ok is None:
        return True
    try:
        return bool(toggle.credentials_ok())
    except Exception:  # noqa: BLE001 — an unreadable probe is a missing key
        return False
