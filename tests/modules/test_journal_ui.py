"""Quartile badge, citation count, the quartile filter and the N+1 guard.

The query-count test is the one that matters long-term: the feed renders up to
100 cards, and `r.paper.journal` is exactly the kind of attribute access that
turns into 100 extra SELECTs the moment someone drops the joinedload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, text
from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.extensions import db as _db
from app.modules.scrape.models import Journal, Paper, UserPaper
from app.modules.scrape.service import list_user_papers, search_user_papers_query

_ISSN = "1234-567X"


@pytest.fixture
def a_user(db):
    from sqlalchemy import text as _text

    user = User.query.filter_by(email="journalui@example.test").first()
    if user is None:
        user = User(
            username="journaluiuser",
            email="journalui@example.test",
            full_name="Journal UI User",
            password_hash=generate_password_hash("password123"),
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
    uid = user.id

    yield user

    db.session.rollback()
    db.session.execute(_text("DELETE FROM user_papers WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(_text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


@pytest.fixture
def clean_content(db):
    yield
    db.session.rollback()
    db.session.execute(text("DELETE FROM user_papers"))
    db.session.execute(text("DELETE FROM papers"))
    db.session.execute(text("DELETE FROM journals"))
    db.session.commit()


@pytest.fixture
def q1_journal(db, clean_content):
    journal = Journal(
        issn_l=_ISSN,
        title="Journal of Examples",
        sjr=Decimal("3.125"),
        sjr_quartile="Q1",
        sjr_year=2025,
    )
    db.session.add(journal)
    db.session.commit()
    return journal


def _paper(db, user, *, issn=_ISSN, cited=42, ext="p1"):
    paper = Paper(
        source="openalex",
        external_id=ext,
        title=f"Paper {ext}",
        abstract="abs",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        issn_l=issn,
        cited_by_count=cited,
    )
    db.session.add(paper)
    db.session.commit()
    db.session.add(UserPaper(user_id=user.id, paper_id=paper.id))
    db.session.commit()
    return paper


# ----------------------------------------------------------------------------
# The relationship
# ----------------------------------------------------------------------------


def test_paper_resolves_its_journal_through_issn(app, db, a_user, q1_journal):
    """No foreign key exists — the relationship joins on the ISSN column."""
    paper = _paper(db, a_user)
    assert paper.journal is not None
    assert paper.journal.sjr_quartile == "Q1"


def test_paper_without_a_seeded_journal_has_none(app, db, a_user, clean_content):
    paper = _paper(db, a_user, issn="9999-9999")
    assert paper.journal is None


def test_paper_without_an_issn_has_no_journal(app, db, a_user, clean_content):
    paper = _paper(db, a_user, issn=None)
    assert paper.journal is None


# ----------------------------------------------------------------------------
# N+1 guard
# ----------------------------------------------------------------------------


def _count_feed_selects(user, expected_rows: int) -> int:
    """SELECTs issued while listing the feed and reading each card's journal."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    # Expire rather than expunge: the objects stay attached (the caller's
    # `user` must remain usable) but every attribute read goes to the
    # database, which is the pessimistic case an N+1 would show up in.
    _db.session.expire_all()
    event.listen(_db.engine, "before_cursor_execute", record)
    try:
        rows = list_user_papers(user, limit=100)
        # Touch exactly what the template touches.
        quartiles = [r.paper.journal.sjr_quartile for r in rows if r.paper.journal]
    finally:
        event.remove(_db.engine, "before_cursor_execute", record)

    assert len(rows) == expected_rows
    assert len(quartiles) == expected_rows
    return len(statements)


def test_feed_journal_lookup_does_not_scale_with_card_count(app, db, a_user, q1_journal):
    """Same guard as the video-summary joinedload: reading `r.paper.journal`
    on every card must not cost one SELECT per card.

    Asserted as "the count does not grow with the number of cards" rather than
    a magic number — the absolute figure includes unrelated eager loads (the
    notes selectinload) that are free to change without this being broken.
    """
    for i in range(3):
        _paper(db, a_user, ext=f"n{i}")
    few = _count_feed_selects(a_user, 3)

    for i in range(3, 15):
        _paper(db, a_user, ext=f"n{i}")
    many = _count_feed_selects(a_user, 15)

    assert many == few, f"query count grew from {few} to {many} with 5x the cards"


# ----------------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------------


def test_quartile_filter_keeps_matching_papers(app, db, a_user, q1_journal):
    _paper(db, a_user, ext="q1paper")
    rows = list_user_papers(a_user, limit=100, quartiles=("Q1",))
    assert [r.paper.external_id for r in rows] == ["q1paper"]


def test_quartile_filter_excludes_other_quartiles(app, db, a_user, q1_journal):
    _paper(db, a_user, ext="q1paper")
    assert list_user_papers(a_user, limit=100, quartiles=("Q2",)) == []


def test_quartile_filter_excludes_papers_with_no_journal(app, db, a_user, q1_journal):
    """ "Show me Q1 work" is a claim about the journal; a paper we know nothing
    about does not satisfy it."""
    _paper(db, a_user, ext="known")
    _paper(db, a_user, ext="unknown", issn="9999-9999")

    rows = list_user_papers(a_user, limit=100, quartiles=("Q1",))
    assert [r.paper.external_id for r in rows] == ["known"]


def test_no_quartile_filter_returns_everything(app, db, a_user, q1_journal):
    _paper(db, a_user, ext="known")
    _paper(db, a_user, ext="unknown", issn="9999-9999")
    assert len(list_user_papers(a_user, limit=100)) == 2


def test_search_query_filters_on_quartile(app, db, a_user, q1_journal):
    _paper(db, a_user, ext="known")
    _paper(db, a_user, ext="unknown", issn="9999-9999")

    rows = search_user_papers_query(a_user, quartile="Q1").all()
    assert [r.paper.external_id for r in rows] == ["known"]


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------


@pytest.fixture
def logged_in(client, a_user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(a_user.id)
        sess["_fresh"] = True
    return client


def test_card_shows_the_quartile_badge_and_citations(logged_in, db, a_user, q1_journal):
    _paper(db, a_user, cited=137)

    body = logged_in.get("/papers/").get_data(as_text=True)
    assert "quartile-q1" in body
    assert "137" in body
    # SJR is CC BY-NC: crediting Scimago wherever a quartile appears is a
    # licence condition, not decoration.
    assert "SCImago" in body


def test_card_omits_the_badge_without_a_journal(logged_in, db, a_user, clean_content):
    _paper(db, a_user, issn="9999-9999")
    body = logged_in.get("/papers/").get_data(as_text=True)
    assert "quartile-q1" not in body


def test_zero_citations_still_renders(logged_in, db, a_user, clean_content):
    """0 is a real, reportable value — `is not none` in the template, not a
    truthiness check."""
    _paper(db, a_user, cited=0, issn="9999-9999")
    body = logged_in.get("/papers/").get_data(as_text=True)
    assert "bi-quote" in body


def test_unknown_citation_count_renders_nothing(logged_in, db, a_user, clean_content):
    _paper(db, a_user, cited=None, issn="9999-9999")
    body = logged_in.get("/papers/").get_data(as_text=True)
    assert "bi-quote" not in body


def test_search_page_rejects_a_bogus_quartile(logged_in, db, a_user, q1_journal):
    """The value reaches a SQL filter, so it is whitelisted rather than passed
    through."""
    _paper(db, a_user, ext="known")
    resp = logged_in.get("/library/search?quartile=Q9%27%20OR%201=1--")
    assert resp.status_code == 200
    # Filter ignored entirely, so the paper is still listed.
    assert "known" in resp.get_data(as_text=True)
