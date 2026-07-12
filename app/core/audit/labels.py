"""Human-readable rendering for audit action keys.

Audit actions are dotted machine keys ("user.totp_failed"). Showing them raw
in the admin log is unfriendly. `humanize_action` turns them into a readable
"Entity · Verb" form without needing a per-action translation entry — the
admin panel is technical enough that this reads fine, and it degrades
gracefully for actions we've never seen before.
"""

from __future__ import annotations

# A few entity prefixes read better with a specific label than a naive
# capitalize(). Everything else falls through to a generic transform.
_ENTITY_LABELS = {
    "user": "User",
    "paper": "Paper",
    "role": "Role",
    "scrape": "Scrape",
    "system_settings": "System",
    "menu": "Menu",
    "permission": "Permission",
}


def humanize_action(action: str | None) -> str:
    """ "user.totp_failed" -> "User · Totp failed". Unknown/blank → as-is."""
    if not action:
        return ""
    if "." not in action:
        return action.replace("_", " ").capitalize()
    entity, verb = action.split(".", 1)
    entity_label = _ENTITY_LABELS.get(entity, entity.replace("_", " ").capitalize())
    verb_label = verb.replace("_", " ").capitalize()
    return f"{entity_label} · {verb_label}"
