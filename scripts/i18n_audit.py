"""Check the catalogs against the source, not just against each other.

CI already compares the TR and EN key sets. That catches a string added to
one catalog and forgotten in the other -- but it is blind to the failure that
actually reached users: a string wrapped in `_()` that never reached *either*
catalog. Both stay equal, CI stays green, and a Turkish reader sees English.
Sixty-six strings were in that state when this script was written, including
the "Read Later" tab and half of the note editor.

Two checks, both read-only:

1. **Coverage.** Every translatable string extracted from `app/` must exist in
   both catalogs.

2. **Shape.** A one- or two-word label whose translation is a full sentence is
   almost always fuzzy-match damage, not a translation. That is how `Notes`
   came to render as "Eşleşme yok." and how the note-delete confirmation came
   to ask whether to delete a *user*. Four entries were in this state; the
   check exists because that damage is silent -- the page renders, the words
   are Turkish, and only someone reading that exact screen notices.

This script **never writes to a catalog.** `pybabel update`'s fuzzy matching
is what caused the damage in check 2 in the first place (CLAUDE.md, "Çeviri İş
Akışı"); extraction for the purpose of *reporting* is the safe half of it.
New strings are added one at a time through the Babel API, by hand.

    venv/Scripts/python.exe scripts/i18n_audit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from babel.messages.extract import DEFAULT_KEYWORDS, extract_from_dir
from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ("tr", "en")

# Mirrors babel.cfg. Kept in step by hand: this is two lines, and importing a
# config parser to read two lines would hide the coupling rather than remove it.
METHODS = [("**.py", "python"), ("**/templates/**.html", "jinja2")]
OPTIONS = {"**/templates/**.html": {"encoding": "utf-8"}}

# Babel's defaults do not include the `_l` alias, and `app/core/_i18n_noop.py`
# -- the file whose entire job is to hold msgids that only ever reach gettext
# through database rows -- is written in `_l`. Without these two keywords this
# audit is blind to exactly the strings that most need an anchor, and it was:
# `Login Email` and `Security (2FA)` sat untranslated in the profile tab strip
# while the audit reported full coverage.
KEYWORDS = {**DEFAULT_KEYWORDS, "_l": None, "lazy_gettext": None}

#: Labels whose translation legitimately reads as a sentence. Empty on purpose
#: -- every entry check 2 found was a defect. Add one only with a note saying
#: why the Turkish needs a full sentence where the English needs a word.
SHAPE_EXCEPTIONS: set[str] = set()

_SENTENCE_END = (".", "!", "?")


def _extract() -> dict[str, str]:
    """msgid -> first source location, for everything under app/."""
    found: dict[str, str] = {}
    for filename, lineno, message, _comments, _context in extract_from_dir(
        str(ROOT / "app"), method_map=METHODS, options_map=OPTIONS, keywords=KEYWORDS
    ):
        msgid = message if isinstance(message, str) else (message or (None,))[0]
        if msgid:
            found.setdefault(msgid, f"{filename}:{lineno}")
    return found


def _catalog(locale: str):
    with open(ROOT / "translations" / locale / "LC_MESSAGES" / "messages.po", "rb") as handle:
        return read_po(handle)


def _ids(catalog) -> set[str]:
    out = set()
    for message in catalog:
        if not message.id:
            continue
        out.add(message.id if isinstance(message.id, str) else message.id[0])
    return out


def check_coverage(found: dict[str, str]) -> list[str]:
    problems = []
    for locale in LOCALES:
        have = _ids(_catalog(locale))
        for msgid in sorted(set(found) - have):
            problems.append(f"{locale}: not in the catalog: {msgid!r}  ({found[msgid]})")
    return problems


def check_shape() -> list[str]:
    """A short label translated as a sentence is fuzzy-match damage."""
    problems = []
    for message in _catalog("tr"):
        msgid, string = message.id, message.string
        if not isinstance(msgid, str) or not isinstance(string, str) or not string:
            continue
        if msgid in SHAPE_EXCEPTIONS:
            continue
        if len(msgid.split()) > 2 or msgid.rstrip().endswith((*_SENTENCE_END, ":")):
            continue
        if string.rstrip().endswith(_SENTENCE_END):
            problems.append(f"tr: short label reads as a sentence: {msgid!r} -> {string!r}")
    return problems


def main() -> int:
    found = _extract()
    problems = check_coverage(found) + check_shape()

    if problems:
        print(f"i18n audit: {len(problems)} problem(s)")
        for line in problems:
            print(f"  {line}")
        print()
        print("Add missing strings with the Babel API, one at a time -- never `pybabel update`.")
        print("See CLAUDE.md, 'Çeviri İş Akışı'.")
        return 1

    print(f"i18n audit: OK — {len(found)} translatable string(s), all present in {len(LOCALES)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
