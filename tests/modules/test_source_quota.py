"""Cumulative weekly source quotas (Faz 5.1).

These run against Postgres, not a stub: the whole point of `consume_quota` is
that the spend and the limit check are one atomic statement, and a fake would
test the mock instead of the guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.modules.scrape import ratelimit
from app.modules.scrape.models import SourceQuotaUsage

_SRC = "test_quota_source"


@pytest.fixture(autouse=True)
def clean_quota_rows(db):
    yield
    SourceQuotaUsage.query.filter_by(source_name=_SRC).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture
def metered(app, monkeypatch):
    """Give `_SRC` a weekly request budget."""

    def configure(requests=None, bytes_=None):
        if requests is not None:
            monkeypatch.setitem(app.config, f"SCRAPE_QUOTA_{_SRC.upper()}_WEEKLY", requests)
        if bytes_ is not None:
            monkeypatch.setitem(app.config, f"SCRAPE_QUOTA_{_SRC.upper()}_WEEKLY_BYTES", bytes_)

    return configure


# ----------------------------------------------------------------------------
# Window arithmetic
# ----------------------------------------------------------------------------


def test_window_starts_on_monday_midnight_utc():
    # A Thursday.
    got = ratelimit.quota_window_start(datetime(2026, 8, 13, 17, 42, 9, tzinfo=UTC))
    assert got == datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)


def test_monday_is_its_own_window_start():
    got = ratelimit.quota_window_start(datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC))
    assert got == datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)


def test_sunday_still_belongs_to_the_week_that_started_monday():
    got = ratelimit.quota_window_start(datetime(2026, 8, 16, 23, 59, 59, tzinfo=UTC))
    assert got == datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)


def test_window_is_utc_not_the_babel_timezone():
    """01:30 Istanbul on Monday is still Sunday 22:30 UTC — i.e. the *previous*
    quota week. The provider's reset boundary is UTC, so this must not follow
    BABEL_DEFAULT_TIMEZONE like BEAT_SCHEDULE does."""
    from zoneinfo import ZoneInfo

    istanbul_monday = datetime(2026, 8, 17, 1, 30, tzinfo=ZoneInfo("Europe/Istanbul"))
    assert ratelimit.quota_window_start(istanbul_monday) == datetime(2026, 8, 10, tzinfo=UTC)


def test_naive_datetime_is_read_as_utc():
    got = ratelimit.quota_window_start(datetime(2026, 8, 13, 12, 0, 0))
    assert got == datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)


# ----------------------------------------------------------------------------
# consume_quota
# ----------------------------------------------------------------------------


def test_unmetered_source_passes_without_touching_the_database(app, db):
    """Every source through Faz 4 has no configured budget and must not start
    writing usage rows nothing reads."""
    assert ratelimit.consume_quota("arxiv") is True
    assert SourceQuotaUsage.query.filter_by(source_name="arxiv").first() is None


def test_first_spend_creates_the_row(app, db, metered):
    metered(requests=10)
    assert ratelimit.consume_quota(_SRC) is True

    row = SourceQuotaUsage.query.filter_by(source_name=_SRC).one()
    assert row.requests_used == 1
    assert row.bytes_used == 0
    assert row.window_start == ratelimit.quota_window_start()


def test_spends_accumulate_within_the_window(app, db, metered):
    metered(requests=10)
    for _ in range(3):
        assert ratelimit.consume_quota(_SRC, cost=2) is True

    row = SourceQuotaUsage.query.filter_by(source_name=_SRC).one()
    assert row.requests_used == 6
    assert SourceQuotaUsage.query.filter_by(source_name=_SRC).count() == 1


def test_spend_is_refused_at_the_limit(app, db, metered):
    metered(requests=3)
    assert ratelimit.consume_quota(_SRC, cost=3) is True
    assert ratelimit.consume_quota(_SRC) is False


def test_refused_spend_is_not_recorded(app, db, metered):
    """A rejected call must not consume budget — otherwise a source that is
    already over its cap keeps burning the next window's headroom."""
    metered(requests=2)
    ratelimit.consume_quota(_SRC, cost=2)
    ratelimit.consume_quota(_SRC, cost=1)

    row = SourceQuotaUsage.query.filter_by(source_name=_SRC).one()
    assert row.requests_used == 2


def test_oversized_single_spend_is_refused(app, db, metered):
    metered(requests=5)
    assert ratelimit.consume_quota(_SRC, cost=99) is False


def test_byte_budget_is_enforced_independently(app, db, metered):
    """EPO OPS meters bandwidth, not calls — a source can be under its request
    ceiling and still out of bytes."""
    metered(requests=1000, bytes_=1000)
    assert ratelimit.consume_quota(_SRC, bytes_=900) is True
    assert ratelimit.consume_quota(_SRC, bytes_=200) is False

    row = SourceQuotaUsage.query.filter_by(source_name=_SRC).one()
    assert row.bytes_used == 900
    assert row.requests_used == 1


def test_byte_only_budget_does_not_reject_every_call(app, db, metered):
    """A byte-metered source leaves the request limit at 0. That must read as
    "no ceiling on requests", not as "zero requests allowed"."""
    metered(bytes_=10_000)
    assert ratelimit.consume_quota(_SRC, cost=1, bytes_=100) is True
    assert ratelimit.consume_quota(_SRC, cost=50, bytes_=100) is True


def test_a_new_window_starts_from_zero(app, db, metered, monkeypatch):
    metered(requests=2)
    assert ratelimit.consume_quota(_SRC, cost=2) is True
    assert ratelimit.consume_quota(_SRC) is False

    # Move the clock into next week; the old row stays as history.
    real_start = ratelimit.quota_window_start
    monkeypatch.setattr(
        ratelimit,
        "quota_window_start",
        lambda now=None: real_start(datetime(2026, 12, 31, tzinfo=UTC)),
    )
    assert ratelimit.consume_quota(_SRC) is True
    assert SourceQuotaUsage.query.filter_by(source_name=_SRC).count() == 2


def test_database_error_fails_closed(app, db, metered, monkeypatch):
    """The opposite of the Redis limiter above it: when we cannot tell whether
    there is budget left, we do not spend a licensed provider's quota."""
    metered(requests=100)

    def boom(*args, **kwargs):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(db.session, "execute", boom)
    assert ratelimit.consume_quota(_SRC) is False


# ----------------------------------------------------------------------------
# quota_usage — admin panel read model
# ----------------------------------------------------------------------------


def test_usage_is_zeroed_before_any_spend(app, db, metered):
    metered(requests=50)
    usage = ratelimit.quota_usage(_SRC)
    assert usage["requests_used"] == 0
    assert usage["requests_limit"] == 50
    assert usage["window_start"] == ratelimit.quota_window_start()


def test_usage_reflects_spending(app, db, metered):
    metered(requests=50, bytes_=500)
    ratelimit.consume_quota(_SRC, cost=4, bytes_=120)

    usage = ratelimit.quota_usage(_SRC)
    assert usage["requests_used"] == 4
    assert usage["bytes_used"] == 120
    assert usage["bytes_limit"] == 500


def test_usage_never_raises(app, db, monkeypatch):
    """This feeds the admin panel, so a broken lookup has to degrade to zeroes
    rather than 500 the page."""

    class _BrokenQuery:
        def filter_by(self, **kwargs):
            raise RuntimeError("no db")

    class Broken:
        query = _BrokenQuery()

    # Patched on the module, not on the real class: `quota_usage` imports the
    # name at call time, and leaving the genuine model intact keeps this
    # file's cleanup fixture working during teardown.
    monkeypatch.setattr("app.modules.scrape.models.SourceQuotaUsage", Broken)
    usage = ratelimit.quota_usage(_SRC)
    assert usage["requests_used"] == 0
