"""How far the templates are from surviving a real `script-src`.

Content-Security-Policy blocks inline event handlers (`onclick=`, `onsubmit=`,
…) exactly as it blocks inline `<script>`. The partial CSP shipped in PR #91
leaves `script-src` out for that reason, and docs/PRELAUNCH.md Y4 tracks the
cleanup module by module.

This file is the ratchet for that cleanup. A directory that has been cleaned
must stay at zero, and the total may fall but not rise — so the work cannot be
quietly undone by the next template someone writes the old way.

The replacements live in app/core/static/js/app.js as delegated listeners:
`data-confirm`, `data-autosubmit`, `data-submit-on-enter`, `data-toggle-abstract`,
`data-bulk-select` and the rest. Reach for those, or add another declarative hook beside them, rather
than an inline handler.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: `hx-on` counts too: htmx runs it through `new Function`, which `script-src`
#: blocks without 'unsafe-eval' -- and base.html turns htmx's eval off.
HANDLER = re.compile(
    r"\son(?:click|dblclick|change|input|submit|reset|keydown|keyup|keypress|"
    r"load|error|focus|blur|mouseover|mouseout|mouseenter|mouseleave)\s*=|\shx-on[:-]",
    re.IGNORECASE,
)

#: Directories whose templates have been cleaned. Each must hold at zero.
CLEAN = [
    "app/core/templates",
    "app/modules",
]

#: The most inline handlers allowed across the whole app. Lower it in the same
#: commit that removes handlers; never raise it. When it reaches zero, the next
#: step is adding `script-src` to the policy in app/__init__.py.
CEILING = 0


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


#: An opening <script> tag without `src=` -- a block of inline code.
INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>", re.IGNORECASE)
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def test_no_inline_script_blocks():
    """Inline <script> is blocked by `script-src` just like an `onclick=`.

    Page code lives in app/core/static/js/app.js or a module's own static/js/
    file, reading anything the template knows -- URLs, translated strings --
    from data-* attributes. Jinja comments are skipped: base.html explains this
    rule in one.
    """
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.html")):
        text = JINJA_COMMENT.sub("", path.read_text(encoding="utf-8"))
        if INLINE_SCRIPT.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "inline <script> blocks:\n" + "\n".join(offenders)


# --------------------------------------------------------------------------
# style="" attributes -- the ratchet for `style-src` (Y4 step 4)
# --------------------------------------------------------------------------

STYLE_ATTR = re.compile(r"\sstyle\s*=", re.IGNORECASE)

#: Directories whose templates hold no style attributes.
STYLE_CLEAN = [
    "app/core/templates",
]

#: Style attributes left across the app. Lower it with each cleanup; never
#: raise it. At zero, `style-src` can join the policy without 'unsafe-inline'.
STYLE_CEILING = 75


def _style_attrs_under(relative: str) -> list[str]:
    """Email templates are exempt: a mail client is not governed by this
    app's CSP, and most of them ignore <style> blocks, so inline is the only
    styling that survives there."""
    found = []
    for path in sorted((ROOT / relative).rglob("*.html")):
        if "email" in path.relative_to(ROOT).parts:
            continue
        text = JINJA_COMMENT.sub("", path.read_text(encoding="utf-8"))
        found += [f"{path.relative_to(ROOT)}: {m.group(0)!r}" for m in STYLE_ATTR.finditer(text)]
    return found


def test_style_clean_directories_stay_clean():
    offenders = [hit for directory in STYLE_CLEAN for hit in _style_attrs_under(directory)]
    assert not offenders, (
        "style attributes in a directory cleaned for CSP -- add a class to "
        "theme.css instead:\n" + "\n".join(offenders)
    )


def test_style_attrs_do_not_grow_and_the_ceiling_is_not_stale():
    total = len(_style_attrs_under("app"))
    assert total <= STYLE_CEILING, f"{total} style attributes, ceiling is {STYLE_CEILING}"
    assert total == STYLE_CEILING, f"{total} remain; lower STYLE_CEILING to {total}"
