"""How far the templates are from surviving a real `script-src`.

Content-Security-Policy blocks inline event handlers (`onclick=`, `onsubmit=`,
…) exactly as it blocks inline `<script>`. The partial CSP shipped in PR #91
leaves `script-src` out for that reason, and docs/PRELAUNCH.md Y4 tracks the
cleanup module by module.

This file is the ratchet for that cleanup. A directory that has been cleaned
must stay at zero, and the total may fall but not rise — so the work cannot be
quietly undone by the next template someone writes the old way.

The replacements live in app/core/static/js/app.js as delegated listeners:
`data-confirm` for confirmation dialogs, `data-remove-on-click` for removing an
element. Reach for those, or add another declarative hook beside them, rather
than an inline handler.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

HANDLER = re.compile(
    r"\son(?:click|dblclick|change|input|submit|reset|keydown|keyup|keypress|"
    r"load|error|focus|blur|mouseover|mouseout|mouseenter|mouseleave)\s*=",
    re.IGNORECASE,
)

#: Directories whose templates have been cleaned. Each must hold at zero.
CLEAN = [
    "app/core/templates",
]

#: The most inline handlers allowed across the whole app. Lower it in the same
#: commit that removes handlers; never raise it. When it reaches zero, the next
#: step is adding `script-src` to the policy in app/__init__.py.
CEILING = 29


def _handlers_under(relative: str) -> list[str]:
    found = []
    for path in sorted((ROOT / relative).rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if HANDLER.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:90]}")
    return found


def test_cleaned_directories_stay_clean():
    offenders = [hit for directory in CLEAN for hit in _handlers_under(directory)]
    assert not offenders, (
        "inline event handlers in a directory that was cleaned for CSP — use a "
        "data-* hook from app.js instead:\n" + "\n".join(offenders)
    )


def test_the_total_does_not_grow():
    total = len(_handlers_under("app"))
    assert total <= CEILING, (
        f"{total} inline event handlers, ceiling is {CEILING}. New ones make a "
        "real script-src further away; use a data-* hook from app.js."
    )


def test_the_ceiling_is_not_stale():
    """A ceiling that trails the real count is not a ratchet.

    If handlers were removed, this fails until CEILING is lowered to match —
    which is what keeps the next removal from being quietly spent on a new
    handler somewhere else.
    """
    total = len(_handlers_under("app"))
    assert total == CEILING, f"{total} handlers remain; lower CEILING to {total}"
