"""Library bulk export — BibTeX + CSV, scoping, and format helpers."""

import csv
import io
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape.models import Paper, UserPaper
from app.modules.scrape.service import papers_to_bibtex, papers_to_csv


@pytest.fixture
def library(db):
    for tbl in ("paper_notes", "user_papers", "papers", "user_roles"):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="exporter").delete()
    db.session.commit()

    u = User(
        username="exporter",
        email="exporter@example.test",
        full_name="Exporter",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()

    # A title with a comma + quotes to prove CSV quoting.
    p1 = Paper(
        source="arxiv",
        external_id="2401.11111",
        title='Attention, "Reloaded": a study',
        abstract="Body with, commas",
        authors=["Ada Lovelace", "Alan Turing"],
        published_at=datetime(2024, 3, 1, tzinfo=UTC),
        categories=["cs.LG"],
    )
    p2 = Paper(
        source="pubmed",
        external_id="99887766",
        title="A Medical Paper",
        abstract="abs",
        authors=["Marie Curie"],
        published_at=datetime(2025, 6, 15, tzinfo=UTC),
        categories=["Gene Editing"],
    )
    db.session.add_all([p1, p2])
    db.session.commit()
    fav = UserPaper(user_id=u.id, paper_id=p1.id, matched_keyword="attention", is_favorite=True)
    plain = UserPaper(user_id=u.id, paper_id=p2.id, matched_keyword="crispr")
    db.session.add_all([fav, plain])
    db.session.commit()

    yield {"user_id": u.id}

    db.session.rollback()
    for tbl in ("paper_notes", "user_papers", "papers", "user_roles"):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="exporter").delete()
    db.session.commit()


def _login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True


# ---------------------------------------------------------------------------
# Format helpers (unit)
# ---------------------------------------------------------------------------


def test_papers_to_bibtex_emits_one_entry_per_paper(db, library):
    rows = UserPaper.query.filter_by(user_id=library["user_id"]).all()
    out = papers_to_bibtex(rows)
    assert out.count("@") == 2
    assert "@misc" in out  # arxiv
    assert "@article" in out  # pubmed


def test_papers_to_csv_quotes_commas_and_quotes(db, library):
    rows = UserPaper.query.filter_by(user_id=library["user_id"]).all()
    out = papers_to_csv(rows)
    parsed = list(csv.reader(io.StringIO(out)))
    assert parsed[0][0] == "source"  # header
    assert len(parsed) == 3  # header + 2 rows
    titles = {r[2] for r in parsed[1:]}
    # The tricky title round-trips through a real CSV parser intact.
    assert 'Attention, "Reloaded": a study' in titles


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_export_bib_downloads_all(client, library):
    _login(client, library["user_id"])
    r = client.get("/library/export.bib?view=all")
    assert r.status_code == 200
    assert r.mimetype == "application/x-bibtex"
    assert "attachment" in r.headers["Content-Disposition"]
    assert r.get_data(as_text=True).count("@") == 2


def test_export_csv_downloads_all(client, library):
    _login(client, library["user_id"])
    r = client.get("/library/export.csv?view=all")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert "scrapemind-all.csv" in r.headers["Content-Disposition"]


def test_export_favorites_only(client, library):
    _login(client, library["user_id"])
    r = client.get("/library/export.bib?view=favorites")
    assert r.status_code == 200
    assert r.get_data(as_text=True).count("@") == 1  # only the favorited paper


def test_export_rejects_unknown_format(client, library):
    _login(client, library["user_id"])
    assert client.get("/library/export.pdf?view=all").status_code == 404


def test_export_rejects_unknown_view(client, library):
    _login(client, library["user_id"])
    assert client.get("/library/export.bib?view=bogus").status_code == 404


def test_export_requires_login(client, library):
    r = client.get("/library/export.bib?view=all", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_export_is_user_scoped(client, db, library):
    """A second user's export must never include the first user's papers."""
    other = User(
        username="other_exporter",
        email="other@example.test",
        full_name="Other",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(other)
    db.session.commit()
    _login(client, other.id)
    r = client.get("/library/export.bib?view=all")
    assert r.status_code == 200
    assert r.get_data(as_text=True).strip() == ""  # no papers of their own
    db.session.query(User).filter_by(username="other_exporter").delete()
    db.session.commit()
