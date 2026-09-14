"""Translation catalogs are compiled, not committed.

The load-bearing properties: a missing or stale .mo is detected and rebuilt,
the build matches what `pybabel compile` produced when the files were
committed, and a local app start compiles them -- because Flask-Babel does not
fail on a missing catalog, it silently serves English.
"""

from __future__ import annotations

import os
import time

from babel.messages.catalog import Catalog
from babel.messages.mofile import read_mo
from babel.messages.pofile import write_po

import app as app_package
from app.core.i18n import catalogs


def _po(root, lang, entries):
    path = root / lang / "LC_MESSAGES" / "messages.po"
    path.parent.mkdir(parents=True)
    cat = Catalog(locale=lang)
    for msgid, string in entries.items():
        cat.add(msgid, string=string)
    with open(path, "wb") as fh:
        write_po(fh, cat)
    return path


def test_a_missing_catalog_is_stale_and_gets_compiled(tmp_path):
    po = _po(tmp_path, "tr", {"Save": "Kaydet"})
    assert catalogs.stale_catalogs(tmp_path) == [po]

    written = catalogs.compile_catalogs(root=tmp_path)
    assert written == [po]
    with open(po.with_suffix(".mo"), "rb") as fh:
        compiled = {m.id: m.string for m in read_mo(fh) if m.id}
    assert compiled == {"Save": "Kaydet"}
    assert catalogs.stale_catalogs(tmp_path) == []


def test_an_edited_po_makes_its_catalog_stale(tmp_path):
    po = _po(tmp_path, "tr", {"Save": "Kaydet"})
    catalogs.compile_catalogs(root=tmp_path)
    later = time.time() + 5
    os.utime(po, (later, later))
    assert catalogs.stale_catalogs(tmp_path) == [po]


def test_fuzzy_entries_are_not_shipped(tmp_path):
    """A fuzzy entry is an unreviewed guess -- how "Save" once read "Aktif"."""
    path = tmp_path / "tr" / "LC_MESSAGES" / "messages.po"
    path.parent.mkdir(parents=True)
    cat = Catalog(locale="tr")
    cat.add("Save", string="Aktif", flags=["fuzzy"])
    with open(path, "wb") as fh:
        write_po(fh, cat)
    catalogs.compile_catalogs(root=tmp_path)
    with open(path.with_suffix(".mo"), "rb") as fh:
        assert "Save" not in {m.id for m in read_mo(fh)}


def test_the_repo_catalogs_are_where_compilation_looks():
    """Guards the path arithmetic in `TRANSLATIONS`: a wrong `parents[n]` would
    compile nothing and report success."""
    assert catalogs.TRANSLATIONS.is_dir()
    assert len(list(catalogs.TRANSLATIONS.glob("*/LC_MESSAGES/messages.po"))) >= 2


def test_app_start_compiles_outside_production(monkeypatch):
    calls = []
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setattr(catalogs, "compile_catalogs", lambda **k: calls.append(k) or [])
    app_package._ensure_translations_compiled()
    assert calls == [{}]


def test_production_start_does_not_write_catalogs(monkeypatch):
    calls = []
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setattr(catalogs, "compile_catalogs", lambda **k: calls.append(k) or [])
    app_package._ensure_translations_compiled()
    assert calls == []


def test_a_compile_failure_never_blocks_start_up(monkeypatch):
    def boom(**k):
        raise OSError("read-only file system")

    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setattr(catalogs, "compile_catalogs", boom)
    app_package._ensure_translations_compiled()  # must not raise
