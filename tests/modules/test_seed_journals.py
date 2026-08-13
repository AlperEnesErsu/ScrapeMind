"""scripts/seed_journals.py — Scimago/DOAJ parsing and idempotent upsert.

Fixtures are written to tmp_path as real CSV, because the parsing quirks being
pinned here are format quirks: semicolon delimiters, comma decimals, ISSNs
without hyphens, and Scimago listing one journal once per subject area.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import seed_journals as sj  # noqa: E402

_SCIMAGO_HEADER = "Title;Issn;Publisher;SJR;SJR Best Quartile;H index;Open Access"


def _scimago(tmp_path, *rows):
    path = tmp_path / "scimago.csv"
    path.write_text("\n".join([_SCIMAGO_HEADER, *rows]), encoding="utf-8")
    return path


@pytest.fixture
def clean_journals(db):
    """Deliberately not autouse: the parsing tests below are pure functions
    over CSV text and must not drag a Postgres connection in behind them."""
    yield
    db.session.rollback()
    db.session.execute(text("DELETE FROM journals"))
    db.session.commit()


# ----------------------------------------------------------------------------
# Field parsing
# ----------------------------------------------------------------------------


def test_issn_gets_its_hyphen_back():
    """Scimago packs ISSNs as 8 bare digits."""
    assert sj.normalise_issn("12345678") == "1234-5678"


def test_issn_keeps_an_x_check_digit():
    assert sj.normalise_issn("1234567X") == "1234-567X"


def test_only_the_first_issn_is_taken():
    """Scimago comma-separates a journal's ISSN formats; `journals` is one row
    per journal."""
    assert sj.normalise_issn("12345678, 87654321") == "1234-5678"


@pytest.mark.parametrize("bad", ["", None, "-", "123", "abcdefgh", "1234567Y"])
def test_malformed_issns_are_rejected(bad):
    assert sj.normalise_issn(bad) is None


def test_decimal_comma_is_converted_to_an_exact_decimal():
    """Decimal, not float (binary rounding) and not str — a string compares
    unequal to the Decimal SQLAlchemy reads back, which quietly made every
    re-run report the row as "updated"."""
    assert sj.parse_decimal("3,125") == Decimal("3.125")
    assert isinstance(sj.parse_decimal("3,125"), Decimal)


def test_unparseable_decimal_is_dropped():
    assert sj.parse_decimal("n/a") is None
    assert sj.parse_decimal("") is None


def test_quartile_accepts_only_q1_to_q4():
    assert sj.parse_quartile("Q1") == "Q1"
    assert sj.parse_quartile("q3") == "Q3"
    # Scimago writes "-" for an unranked journal.
    assert sj.parse_quartile("-") is None
    assert sj.parse_quartile("") is None


# ----------------------------------------------------------------------------
# Reading the files
# ----------------------------------------------------------------------------


def test_reads_a_scimago_row(tmp_path):
    path = _scimago(tmp_path, "Journal of Examples;12345678;Example Press;3,125;Q1;120;FALSE")
    records = sj.read_scimago(path, year=2025)

    assert list(records) == ["1234-5678"]
    row = records["1234-5678"]
    assert row["title"] == "Journal of Examples"
    assert row["publisher"] == "Example Press"
    assert row["sjr"] == Decimal("3.125")
    assert row["sjr_quartile"] == "Q1"
    assert row["h_index"] == 120
    assert row["sjr_year"] == 2025
    assert row["is_oa"] is False


def test_open_access_flag_is_read(tmp_path):
    path = _scimago(tmp_path, "Open Journal;12345678;Press;1,0;Q2;10;TRUE")
    assert sj.read_scimago(path, year=None)["1234-5678"]["is_oa"] is True


def test_rows_without_a_usable_issn_are_skipped(tmp_path):
    path = _scimago(
        tmp_path,
        "No ISSN;-;Press;1,0;Q2;10;FALSE",
        "Good;12345678;Press;1,0;Q1;10;FALSE",
    )
    assert list(sj.read_scimago(path, year=None)) == ["1234-5678"]


def test_a_journal_listed_once_per_subject_area_collapses(tmp_path):
    """Scimago repeats a journal per subject area with identical journal-level
    values, so any of the duplicates is equally correct."""
    path = _scimago(
        tmp_path,
        "Journal of Examples;12345678;Press;3,125;Q1;120;FALSE",
        "Journal of Examples;12345678;Press;3,125;Q1;120;FALSE",
    )
    records = sj.read_scimago(path, year=2025)
    assert len(records) == 1


def test_doaj_collects_both_issn_columns(tmp_path):
    path = tmp_path / "doaj.csv"
    path.write_text(
        "Journal ISSN (print version),Journal EISSN (online version)\n12345678,87654321\n",
        encoding="utf-8",
    )
    assert sj.read_doaj(path) == {"1234-5678", "8765-4321"}


# ----------------------------------------------------------------------------
# Upsert
# ----------------------------------------------------------------------------


def _record(**overrides):
    base = {
        "title": "Journal of Examples",
        "publisher": "Example Press",
        "sjr": Decimal("3.125"),
        "sjr_quartile": "Q1",
        "sjr_year": 2025,
        "h_index": 120,
        "is_oa": False,
    }
    base.update(overrides)
    return {"1234-5678": base}


def test_upsert_creates_rows(app, db, clean_journals):
    from app.modules.scrape.models import Journal

    created, updated = sj.upsert_journals(_record(), set())
    assert (created, updated) == (1, 0)

    row = Journal.query.filter_by(issn_l="1234-5678").one()
    assert row.sjr_quartile == "Q1"
    assert float(row.sjr) == 3.125


def test_rerunning_updates_instead_of_duplicating(app, db, clean_journals):
    """A new annual snapshot is applied by simply running the script again."""
    from app.modules.scrape.models import Journal

    sj.upsert_journals(_record(), set())
    created, updated = sj.upsert_journals(_record(sjr_quartile="Q2", sjr_year=2026), set())

    assert (created, updated) == (0, 1)
    assert Journal.query.count() == 1
    assert Journal.query.one().sjr_quartile == "Q2"


def test_identical_rerun_reports_no_change(app, db, clean_journals):
    sj.upsert_journals(_record(), set())
    assert sj.upsert_journals(_record(), set()) == (0, 0)


def test_a_missing_value_does_not_wipe_an_existing_one(app, db, clean_journals):
    """Same reasoning as the fill-only enrichment in service._enrich."""
    from app.modules.scrape.models import Journal

    sj.upsert_journals(_record(), set())
    sj.upsert_journals(_record(h_index=None, sjr=None), set())

    row = Journal.query.one()
    assert row.h_index == 120
    assert float(row.sjr) == 3.125


def test_doaj_membership_is_applied(app, db, clean_journals):
    from app.modules.scrape.models import Journal

    sj.upsert_journals(_record(), {"1234-5678"})
    assert Journal.query.one().is_doaj is True


def test_journal_absent_from_a_new_export_is_kept(app, db, clean_journals):
    """Scimago drops journals between editions; deleting the row would
    silently strip the badge from papers that legitimately carry that ISSN."""
    from app.modules.scrape.models import Journal

    sj.upsert_journals(_record(), set())
    sj.upsert_journals({"9999-9999": _record()["1234-5678"]}, set())

    assert Journal.query.count() == 2
    assert Journal.query.filter_by(issn_l="1234-5678").one() is not None
