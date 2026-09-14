"""Compile translations/*/LC_MESSAGES/messages.po into .mo files.

Compiled catalogs are build output and are not committed. Every consumer
compiles them: the Docker image at build time, CI before it renders pages or
runs tests, the test suite when a catalog is stale, and the app itself on
start-up outside production.

They were committed until September 2026, and every branch that touched a
translation produced a binary merge conflict on them -- three in one week on
the patent work alone, each resolved by regenerating the file anyway.

Babel API rather than `pybabel compile`, so no console entry point is needed;
the output was checked byte-for-byte against `pybabel compile`. Never
`pybabel update` -- see CLAUDE.md.
"""

from __future__ import annotations

from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

TRANSLATIONS = Path(__file__).resolve().parents[3] / "translations"


def stale_catalogs(root: Path = TRANSLATIONS) -> list[Path]:
    """.po files whose .mo is missing or older than the .po."""
    out = []
    for po in sorted(root.glob("*/LC_MESSAGES/messages.po")):
        mo = po.with_suffix(".mo")
        if not mo.exists() or mo.stat().st_mtime < po.stat().st_mtime:
            out.append(po)
    return out


def compile_catalogs(*, force: bool = False, root: Path = TRANSLATIONS) -> list[Path]:
    """Compile stale (or, with `force`, all) catalogs. Returns what was written."""
    targets = sorted(root.glob("*/LC_MESSAGES/messages.po")) if force else stale_catalogs(root)
    for po in targets:
        with open(po, "rb") as fh:
            catalog = read_po(fh)
        with open(po.with_suffix(".mo"), "wb") as fh:
            # `use_fuzzy=False`: a fuzzy entry is an unreviewed guess, and
            # shipping it is how "Save" once read "Aktif".
            write_mo(fh, catalog, use_fuzzy=False)
    return targets
