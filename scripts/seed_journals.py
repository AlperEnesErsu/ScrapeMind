"""Seed the `journals` table from Scimago (SJR) and, optionally, DOAJ.

Usage:
    venv/Scripts/python.exe scripts/seed_journals.py --scimago scimagojr-2025.csv
    venv/Scripts/python.exe scripts/seed_journals.py --scimago sj.csv --doaj doaj.csv
    venv/Scripts/python.exe scripts/seed_journals.py --scimago sj.csv --year 2025

Where the files come from
-------------------------
Scimago publishes the ranking as a semicolon-separated CSV at
https://www.scimagojr.com/journalrank.php (the "Download data" link, or
`journalrank.php?out=xls` — despite the name, the payload is CSV). DOAJ
publishes its full index as CSV at https://doaj.org/csv.

Both are downloaded **by hand** rather than fetched here on purpose: they are
large annual/periodic snapshots, not something a nightly task should pull, and
neither offers a stable versioned URL worth hard-coding into a scraper.

Licensing — read before shipping a change
-----------------------------------------
SJR data is **CC BY-NC with attribution required**. Two consequences that are
not optional:

  * Anywhere a quartile or SJR value is displayed, Scimago must be credited.
    The UI does this next to the quartile badge; see `docs/SCRAPING.md` §11.
  * The NC clause holds while ScrapeMind is non-commercial. If that ever
    changes, this dependency has to be re-evaluated — it is the one piece of
    data here that a commercial licence would not automatically cover.

Behaviour
---------
Idempotent: re-running updates existing rows (matched on `issn_l`) rather than
duplicating them, so a new annual snapshot is applied by simply running it
again. Rows already present but missing from the new file are left alone —
Scimago drops journals between editions, and deleting a row would silently
strip the quality badge from papers that legitimately carry that ISSN.
"""

from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.modules.scrape.models import Journal  # noqa: E402

#: Scimago ships semicolon-separated values with a comma decimal separator
#: ("3,125"), which is why the numbers are parsed by hand below rather than
#: with float() directly.
_SCIMAGO_DELIMITER = ";"

_QUARTILES = {"Q1", "Q2", "Q3", "Q4"}


def normalise_issn(raw: str | None) -> str | None:
    """`"12345678"` / `"1234-5678"` -> `"1234-5678"`, or None.

    Scimago packs ISSNs as 8 digits with no hyphen, several per journal,
    comma-separated ("12345678, 87654321"). Only the first is taken: the
    others are the same journal's other formats, and `journals.issn_l` is one
    row per journal.
    """
    if not raw:
        return None
    candidate = raw.split(",")[0].strip().upper().replace("-", "")
    if len(candidate) != 8:
        return None
    if not candidate[:7].isdigit():
        return None
    if not (candidate[7].isdigit() or candidate[7] == "X"):
        return None
    return f"{candidate[:4]}-{candidate[4:]}"


def parse_decimal(raw: str | None) -> Decimal | None:
    """Scimago writes decimals with a comma ("3,125").

    Returns `Decimal`, not `float` or `str`. Float would reintroduce the
    binary rounding the Numeric column exists to avoid; a string would compare
    unequal to the `Decimal` SQLAlchemy reads back, which quietly made every
    re-run report the row as "updated".
    """
    if not raw:
        return None
    text = raw.strip().replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_int(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


def parse_quartile(raw: str | None) -> str | None:
    """ "Q1" -> "Q1"; "-" (Scimago's "not ranked") -> None."""
    value = (raw or "").strip().upper()
    return value if value in _QUARTILES else None


def read_scimago(path: Path, *, year: int | None) -> dict[str, dict]:
    """Parse a Scimago export into `{issn_l: field dict}`.

    Later rows win on a duplicate ISSN. That is not arbitrary: Scimago lists a
    journal once per subject area, and the rows carry identical journal-level
    values, so any of them is equally correct.
    """
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh, delimiter=_SCIMAGO_DELIMITER):
            issn = normalise_issn(row.get("Issn"))
            title = (row.get("Title") or "").strip()
            if not issn or not title:
                continue
            out[issn] = {
                "title": title,
                "publisher": (row.get("Publisher") or "").strip() or None,
                "sjr": parse_decimal(row.get("SJR")),
                "sjr_quartile": parse_quartile(row.get("SJR Best Quartile")),
                "sjr_year": year,
                "h_index": parse_int(row.get("H index")),
                # Scimago's own OA flag. DOAJ membership is a stricter claim
                # and is applied separately below.
                "is_oa": (row.get("Open Access") or "").strip().upper() in ("TRUE", "YES", "1"),
            }
    return out


def read_doaj(path: Path) -> set[str]:
    """ISSNs listed in the DOAJ index.

    DOAJ gives each journal a print and an electronic ISSN in separate
    columns; both are collected, because a paper may carry either and we only
    want to know "is this journal in DOAJ".
    """
    issns: set[str] = set()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            for column in ("Journal ISSN (print version)", "Journal EISSN (online version)"):
                issn = normalise_issn(row.get(column))
                if issn:
                    issns.add(issn)
    return issns


def upsert_journals(records: dict[str, dict], doaj_issns: set[str]) -> tuple[int, int]:
    """Insert or update one row per ISSN. Returns `(created, updated)`.

    Existing rows absent from `records` are left untouched — Scimago drops
    journals between editions, and removing a row would silently strip the
    quality badge from papers that legitimately carry that ISSN.
    """
    existing = {j.issn_l: j for j in Journal.query.all()}
    created = updated = 0

    for issn, fields in records.items():
        fields = dict(fields, is_doaj=issn in doaj_issns)
        row = existing.get(issn)
        if row is None:
            db.session.add(Journal(issn_l=issn, **fields))
            created += 1
            continue
        changed = False
        for key, value in fields.items():
            # A new export that omits a value must not wipe one we already
            # have — same reasoning as the fill-only enrichment in
            # service._enrich.
            if value is None:
                continue
            if getattr(row, key) != value:
                setattr(row, key, value)
                changed = True
        if changed:
            updated += 1

    db.session.commit()
    return created, updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scimago", required=True, type=Path, help="Scimago CSV export")
    parser.add_argument("--doaj", type=Path, help="DOAJ CSV export (optional)")
    parser.add_argument(
        "--year",
        type=int,
        help="Ranking year recorded on each row. The Scimago export does not "
        "carry it, and a quartile without a year implies a timeless verdict.",
    )
    args = parser.parse_args()

    if not args.scimago.exists():
        print(f"Scimago file not found: {args.scimago}", file=sys.stderr)
        return 1
    if args.doaj and not args.doaj.exists():
        print(f"DOAJ file not found: {args.doaj}", file=sys.stderr)
        return 1

    records = read_scimago(args.scimago, year=args.year)
    if not records:
        print("No usable rows found — is this a Scimago export?", file=sys.stderr)
        return 1
    doaj_issns = read_doaj(args.doaj) if args.doaj else set()

    app = create_app()
    with app.app_context():
        created, updated = upsert_journals(records, doaj_issns)

    print(f"journals: {created} created, {updated} updated (from {len(records)} rows)")
    if doaj_issns:
        print(f"DOAJ: {len(doaj_issns)} ISSNs matched against the index")
    print("Data: SCImago, (n.d.). SJR — SCImago Journal & Country Rank. CC BY-NC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
