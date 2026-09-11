"""Check the catalogs against the source, not just against each other.

CI already compares the TR and EN key sets. That catches a string added to
one catalog and forgotten in the other -- but it is blind to the failure that
actually reached users: a string wrapped in `_()` that never reached *either*
catalog. Both stay equal, CI stays green, and a Turkish reader sees English.
Sixty-six strings were in that state when this script was written, including
the "Read Later" tab and half of the note editor.

Three checks, all read-only:

1. **Coverage.** Every translatable string extracted from `app/` must exist in
   both catalogs -- plus the labels that reach gettext through data rather
   than through a literal call, which extraction cannot see at all.

2. **Shape.** A one- or two-word label whose translation is a full sentence is
   almost always fuzzy-match damage, not a translation. That is how `Notes`
   came to render as "Eşleşme yok." and how the note-delete confirmation came
   to ask whether to delete a *user*. Four entries were in this state; the
   check exists because that damage is silent -- the page renders, the words
   are Turkish, and only someone reading that exact screen notices.

3. **Duplication.** One Turkish string attached to two msgids is the
   signature of the same damage in its other, quieter form: `Toggle favorite`
   read "Tema Değiştir", inherited from the dark-mode switch on its way out,
   and `Save` read "Aktif". Twenty entries were in this state and check 2
   could not see any of them, because the words are ordinary short labels --
   for something else.

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
sys.path.insert(0, str(ROOT))  # so `_dynamic_labels` can import the app's own data

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


def _dynamic_labels() -> dict[str, str]:
    """Strings that reach gettext through data, not through a literal call.

    `{{ _(opt.desc) }}` renders a source description through a variable, so
    static extraction cannot see it and the string is translated only if it
    happens to be in the catalog already. Four were not: OpenAlex, Crossref,
    YouTube channels and Scopus all showed English text in a Turkish sidebar,
    next to five siblings that showed Turkish.

    The usual answer is `app/core/_i18n_noop.py` -- a hand-kept list of such
    msgids. That list is what drifted: each of the four came in with a later
    phase and nobody copied it across. So this reads the data itself. A source
    added tomorrow is checked tomorrow, with nothing to remember.
    """
    from app.modules.scrape.sources import SOURCE_META, TOPICS

    out: dict[str, str] = {}
    for name, meta in SOURCE_META.items():
        desc = meta.get("desc")
        if desc:
            out.setdefault(desc, f"SOURCE_META[{name!r}]['desc']")
    for key, label in TOPICS.items():
        if label:
            out.setdefault(label, f"TOPICS[{key!r}]")
    return out


#: Turkish strings that more than one msgid may legitimately share. Synonyms
#: in English that are one word in Turkish (Abstract/Summary -> Özet), and the
#: `menu.*` keys, which exist to label the same thing as their English sibling.
#: Everything not listed here is treated as fuzzy-match damage, because when
#: this check was written everything not listed here *was*.
DUPLICATE_EXCEPTIONS: set[str] = {
    "Özet",  # Abstract / Summary
    "Atıf",  # Cite / Citation
    "Denetim Günlüğü",  # Audit Log / Audit log
    "Keşfet",  # Discover / Open discover / menu.discover
    "Kütüphanem",  # My library / menu.library
    "Notlarım",  # My notes / menu.library.notes
    "Raporlar",  # Reports / menu.reports
    "Sistem Ayarları",  # System Settings / menu.system
    "Zaman",  # Time / Timeline -- the tab reads correctly as either
    "İlgi alanı ekle",  # Add interests / New interest
}


def check_duplicates() -> list[str]:
    """One Turkish string on two msgids is the signature of fuzzy matching.

    `pybabel update` hands a removed string's translation to whichever
    surviving msgid looks most similar to it. `Toggle Theme` went out with dark
    mode and its translation landed on `Toggle favorite`, so the favourite
    button's tooltip and screen-reader label read "Tema Değiştir". `Delete this
    note?` inherited the role dialog's text and asked whether to delete a
    *role*. `Save` read "Aktif".

    Twenty entries were in this state, and none of them were visible to check
    2: the translations are not sentences, they are perfectly ordinary short
    labels -- for something else. Several sit in `title` and `aria-label`,
    where nobody reads them until a screen reader does.
    """
    seen: dict[str, list[str]] = {}
    for message in _catalog("tr"):
        msgid, string = message.id, message.string
        if isinstance(msgid, str) and isinstance(string, str) and string:
            seen.setdefault(string, []).append(msgid)

    problems = []
    for string, msgids in sorted(seen.items()):
        if len(msgids) > 1 and string not in DUPLICATE_EXCEPTIONS:
            problems.append(f"tr: one translation on {len(msgids)} msgids: {string!r} <- {msgids}")
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
    found = {**_extract(), **_dynamic_labels()}
    problems = check_coverage(found) + check_shape() + check_duplicates()

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
