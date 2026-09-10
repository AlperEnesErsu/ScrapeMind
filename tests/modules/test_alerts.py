"""Saved searches and their alerts (Faz 7.1).

The test that matters most is `test_a_paper_that_becomes_a_match_is_announced`.
Everything else here could pass with a timestamp watermark; that one is the
reason there is a notified-set table instead, and it is the behaviour a future
"simplification" would break first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.models.notification import Notification
from app.extensions import db as _db
from app.modules.scrape import alerts
from app.modules.scrape.models import Paper, SavedSearch, SavedSearchNotification, UserPaper


@pytest.fixture
def user(db):
    """A fresh user, unique per test.

    Deliberately does not clear the users table the way tests/core does: rows
    from other test modules are harmless here, and every assertion below is
    scoped to this user's own library anyway.
    """
    from app.core.models.user import User

    suffix = uuid.uuid4().hex[:8]
    u = User(
        username=f"alerts-{suffix}",
        email=f"alerts-{suffix}@example.test",
        full_name="Alerts tester",
        password_hash="x",
        locale="tr",
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def library(db, user):
    """Put a paper in the user's library and hand it back."""

    def _add(*, title: str, abstract: str | None = None, created_days_ago: int = 0) -> Paper:
        paper = Paper(
            source="openalex",
            external_id=f"W{uuid.uuid4().hex[:12]}",
            title=title,
            abstract=abstract,
            published_at=datetime.now(UTC) - timedelta(days=created_days_ago),
        )
        db.session.add(paper)
        db.session.flush()
        db.session.add(UserPaper(user_id=user.id, paper_id=paper.id))
        db.session.commit()
        return paper

    return _add


@pytest.fixture
def saved_search(db, user):
    def _make(*, q: str, name: str | None = None, **kwargs) -> SavedSearch:
        search = SavedSearch(
            user_id=user.id,
            name=name or f"search-{uuid.uuid4().hex[:8]}",
            q=q,
            **kwargs,
        )
        db.session.add(search)
        db.session.commit()
        return search

    return _make


# --------------------------------------------------------------------------
# The reason this feature has a table instead of a timestamp
# --------------------------------------------------------------------------


def test_a_paper_that_becomes_a_match_is_announced(db, library, saved_search):
    """The case a watermark drops silently.

    The paper is already in the library and already old when the search first
    runs -- it simply does not match yet, because its abstract is empty. Later
    a DOI match fills that abstract in (or, in Faz 7.0's case, the OA full text
    arrives). Nothing about the paper is new. It is the moment the user asked
    to be told about.
    """
    paper = library(title="Thermal management in stacked dies", created_days_ago=90)
    search = saved_search(q="pomegranate")

    first = alerts.find_new_matches(search)
    assert first.count == 0, "does not match yet"
    alerts.run_saved_search(search)

    # Enrichment fills in what was missing. Note the paper's own timestamps are
    # untouched -- a watermark on last_run_at would never look at it again.
    paper.abstract = "A study of pomegranate-shaped heat spreaders."
    db.session.commit()

    second = alerts.find_new_matches(search)
    assert second.count == 1
    assert second.papers[0].id == paper.id


def test_the_same_paper_is_never_announced_twice(db, library, saved_search):
    library(title="Pomegranate seeds and packing density")
    search = saved_search(q="pomegranate")

    first = alerts.run_saved_search(search)
    assert first is not None and first.count == 1

    assert alerts.run_saved_search(search) is None, "nothing left to say"


def test_announcing_is_idempotent_under_a_concurrent_run(db, library, saved_search):
    """Two overlapping runs -- a manual trigger during the nightly sweep --
    must not both announce. The unique constraint is what makes that true,
    rather than the check-then-write above it."""
    library(title="Pomegranate arils")
    search = saved_search(q="pomegranate")

    result = alerts.find_new_matches(search)
    assert alerts.announce(result) == 1
    # Same result object replayed, exactly as a second worker would.
    assert alerts.announce(result) == 0

    rows = SavedSearchNotification.query.filter_by(saved_search_id=search.id).count()
    assert rows == 1


# --------------------------------------------------------------------------
# Noise
# --------------------------------------------------------------------------


def test_a_broad_search_is_capped_and_says_so(db, library, saved_search, monkeypatch):
    """A flood is what makes a user switch alerts off, so the cap is part of
    the feature rather than a safety valve."""
    monkeypatch.setattr(alerts, "MAX_MATCHES_PER_RUN", 3)
    for i in range(5):
        library(title=f"Pomegranate study {i}")
    search = saved_search(q="pomegranate")

    result = alerts.find_new_matches(search)
    assert result.count == 3
    assert result.capped is True

    _title, message = alerts.notification_text(result)
    assert "3" in message, "the cap should be named in the text"


def test_one_notification_per_run_not_per_paper(db, library, saved_search, user):
    for i in range(4):
        library(title=f"Pomegranate paper {i}")
    search = saved_search(q="pomegranate", name="Pomegranates")

    before = Notification.query.filter_by(user_id=user.id).count()
    result = alerts.run_saved_search(search)
    after = Notification.query.filter_by(user_id=user.id).count()

    assert result.count == 4
    assert after - before == 1, "four papers, one notification"

    latest = Notification.query.filter_by(user_id=user.id).order_by(Notification.id.desc()).first()
    assert "Pomegranates" in latest.title


def test_an_empty_run_writes_no_notification(db, saved_search, user):
    """ "Never call for nothing" -- the same contract the digest keeps."""
    search = saved_search(q="nothing-matches-this")

    before = Notification.query.filter_by(user_id=user.id).count()
    assert alerts.run_saved_search(search) is None
    assert Notification.query.filter_by(user_id=user.id).count() == before
    assert search.last_run_at is not None, "an empty run is still a run"


# --------------------------------------------------------------------------
# Cadence and safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cadence", ["off"])
def test_an_off_search_never_runs(db, library, saved_search, cadence):
    library(title="Pomegranate paper")
    search = saved_search(q="pomegranate", cadence=cadence)
    assert alerts.run_saved_search(search) is None
    assert SavedSearchNotification.query.filter_by(saved_search_id=search.id).count() == 0


def test_an_inactive_search_never_runs(db, library, saved_search):
    library(title="Pomegranate paper")
    search = saved_search(q="pomegranate", is_active=False)
    assert alerts.run_saved_search(search) is None


def test_unknown_filter_keys_are_ignored_not_forwarded(db, saved_search):
    """`filters` is user-editable JSON reaching a query builder.

    Forwarding an unrecognised key would turn a typo into a TypeError inside a
    worker at four in the morning.
    """
    search = saved_search(q="x", filters={"source": "openalex", "drop_table": "papers"})
    assert alerts._filter_kwargs(search) == {"source": "openalex"}


def test_empty_filter_values_are_dropped(db, saved_search):
    """A form posts empty strings; passing them through would filter on ''."""
    search = saved_search(q="x", filters={"source": "", "quartile": None, "has_notes": True})
    assert alerts._filter_kwargs(search) == {"has_notes": True}


def test_searches_are_scoped_to_their_owner(db, library, saved_search, user):
    """A saved search reads the owner's library, so another user's papers can
    never appear in it."""
    from app.core.models.user import User

    suffix = uuid.uuid4().hex[:8]
    other = User(
        username=f"other-{suffix}",
        email=f"other-{suffix}@example.test",
        full_name="Someone else",
        password_hash="x",
    )
    _db.session.add(other)
    _db.session.commit()

    library(title="Pomegranate paper")  # belongs to `user`
    theirs = SavedSearch(user_id=other.id, name="theirs", q="pomegranate")
    _db.session.add(theirs)
    _db.session.commit()

    assert alerts.find_new_matches(theirs).count == 0
