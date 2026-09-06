"""Faz 6 — retrospective reports: create_report validation/quota and the
run_report topic + author_group map/reduce pipelines.

No real network calls: `openalex_source.aggregate_works`/`works_in_range`/
`works_by_author` and `ai_service.summarize_report_chunk`/
`synthesize_report`/`is_ai_enabled` are monkeypatched throughout, same
pattern as tests/modules/test_digest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from flask_babel import gettext as _
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.service import add_user_keyword
from app.modules.scrape import ai_service, report_service, service
from app.modules.scrape.models import Journal, Paper, Report, UserAuthor, UserPaper
from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.sources import openalex_source as oa
from app.modules.scrape.sources.payload import PaperPayload


def _payload(
    ext_id: str,
    *,
    title: str = "A Title",
    year: int | None = 2024,
    cited: int | None = 5,
    issn_l: str | None = None,
) -> PaperPayload:
    return PaperPayload(
        source="openalex",
        external_id=ext_id,
        title=title,
        abstract="An abstract about the paper's method and findings.",
        authors=["A. One"],
        url=f"https://openalex.org/{ext_id}",
        pdf_url=None,
        published_at=datetime(year, 6, 1, tzinfo=UTC) if year is not None else None,
        categories=["cs.LG"],
        issn_l=issn_l,
        cited_by_count=cited,
    )


_DEFAULT_AGGREGATE = {
    "publication_year": [
        {"key": "2023", "name": "2023", "count": 3},
        {"key": "2024", "name": "2024", "count": 5},
    ],
    "authorships.author.id": [{"key": "A1", "name": "Author One", "count": 4}],
    "primary_location.source.id": [{"key": "S1", "name": "Venue One", "count": 4}],
    "primary_topic.id": [{"key": "T1", "name": "Topic One", "count": 4}],
    # The real shape: OpenAlex keys the boolean "1"/"0" and puts the words in
    # key_display_name (-> `name`). The fixture used to say key="true", which
    # is what let a 0%-OA bug through every test in this file.
    "open_access.is_oa": [
        {"key": "1", "name": "true", "count": 6},
        {"key": "0", "name": "false", "count": 2},
    ],
}


def _fake_aggregate_works(query, *, since_year, until_year, group_by):
    return _DEFAULT_AGGREGATE.get(group_by, [])


# Captured before the autouse fixture below ever monkeypatches these names,
# so a test that needs the *real* is_ai_enabled-gated behavior (see
# test_run_report_ai_disabled_still_produces_a_stats_only_report) can put the
# genuine implementation back for just that one test.
_REAL_SUMMARIZE_CHUNK = ai_service.summarize_report_chunk
_REAL_SYNTHESIZE_REPORT = ai_service.synthesize_report
_REAL_SYNTHESIZE_GROUP = ai_service.synthesize_author_group_report

_FAKE_CHUNK_SUMMARY = {"themes": ["x"], "notable": [], "methods": []}
_FAKE_SECTIONS = {
    "tldr": "Özet.",
    "timeline": [],
    "emerging": [],
    "fading": [],
    "key_works": [],
    "key_authors": [],
    "key_venues": [],
    "for_your_keywords": "",
}


@pytest.fixture
def clean_user(db):
    for tbl in (
        "notifications",
        "user_digests",
        "reports",
        "author_group_members",
        "user_authors",
        "author_groups",
        "paper_notes",
        "user_papers",
        "papers",
        "journals",
        "user_settings",
        "user_keywords",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="reportuser").delete()
    db.session.commit()
    u = User(
        username="reportuser",
        email="reportuser@example.test",
        full_name="Report User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    for tbl in (
        "notifications",
        "user_digests",
        "reports",
        "author_group_members",
        "user_authors",
        "author_groups",
        "paper_notes",
        "user_papers",
        "papers",
        "journals",
        "user_settings",
        "user_keywords",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(id=u.id).delete()
    db.session.commit()


@pytest.fixture(autouse=True)
def _default_stubs(monkeypatch):
    """Applied to every test in this file: a harmless default for every
    external call, so a test only needs to override what it actually cares
    about. Real network calls are never reachable here."""
    monkeypatch.setattr(oa, "aggregate_works", _fake_aggregate_works)
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: [])
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(
        ai_service,
        "summarize_report_chunk",
        lambda items, *, context, user=None: _FAKE_CHUNK_SUMMARY,
    )
    monkeypatch.setattr(
        ai_service,
        "synthesize_report",
        lambda chunk_summaries, stats, *, user=None: dict(_FAKE_SECTIONS),
    )
    # The author-group dossier has its own reduce prompt/schema — organised
    # by person, not by year — so it needs its own stub rather than borrowing
    # the topic report's.
    monkeypatch.setattr(
        ai_service,
        "synthesize_author_group_report",
        lambda member_summaries, stats, *, user=None: {
            "tldr": "Grup özeti.",
            "members": [
                {"name": name, "focus": f"{name} şunu çalışıyor.", "recent_highlights": ["not"]}
                for name in (member_summaries or {})
            ],
            "shared_topics": ["ortak konu"],
            "overlap_with_you": "Senin anahtar kelimelerinle kesişiyor.",
            "coauthorship_notes": "Ortak yazarlık notu.",
        },
    )


# ----------------------------------------------------------------------------
# create_report — validation + quota
# ----------------------------------------------------------------------------


def test_create_report_rejects_invalid_kind(app, db, clean_user):
    with app.app_context():
        row, error = report_service.create_report(clean_user, "bogus", {})
        assert row is None
        assert str(error) == str(report_service.REPORT_KIND_INVALID_MESSAGE)


def test_create_report_topic_requires_keywords(app, db, clean_user):
    with app.app_context():
        row, error = report_service.create_report(clean_user, "topic", {"keywords": [], "years": 3})
        assert row is None
        assert str(error) == str(report_service.REPORT_KEYWORDS_REQUIRED_MESSAGE)


def test_create_report_topic_requires_keywords_key_present(app, db, clean_user):
    with app.app_context():
        row, error = report_service.create_report(clean_user, "topic", {"years": 3})
        assert row is None
        assert str(error) == str(report_service.REPORT_KEYWORDS_REQUIRED_MESSAGE)


def test_create_report_topic_rejects_year_span_too_small(app, db, clean_user):
    with app.app_context():
        row, error = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 0}
        )
        assert row is None
        expected = _(
            "Year range must be between %(min)s and %(max)s years.",
            min=report_service.REPORT_MIN_YEARS,
            max=report_service.REPORT_MAX_YEARS,
        )
        assert str(error) == str(expected)


def test_create_report_topic_rejects_year_span_too_large(app, db, clean_user):
    with app.app_context():
        row, error = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 99}
        )
        assert row is None


def test_create_report_persists_pending_row(app, db, clean_user):
    with app.app_context():
        row, error = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety", "alignment"], "years": 3}
        )
        assert error is None
        assert row is not None
        assert row.status == "pending"
        assert row.kind == "topic"
        assert row.params["keywords"] == ["ai safety", "alignment"]
        assert row.params["years"] == 3
        assert "ai safety" in row.title


def test_create_report_enforces_daily_cap(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(report_service, "MAX_REPORTS_PER_DAY", 2)
    with app.app_context():
        r1, e1 = report_service.create_report(clean_user, "topic", {"keywords": ["a"], "years": 1})
        r2, e2 = report_service.create_report(clean_user, "topic", {"keywords": ["b"], "years": 1})
        assert e1 is None and e2 is None
        assert r1 is not None and r2 is not None

        r3, e3 = report_service.create_report(clean_user, "topic", {"keywords": ["c"], "years": 1})
        assert r3 is None
        assert str(e3) == str(report_service.REPORT_DAILY_CAP_MESSAGE)
        assert Report.query.filter_by(user_id=clean_user.id).count() == 2


# ----------------------------------------------------------------------------
# run_report — unsupported kind
# ----------------------------------------------------------------------------


def test_run_report_unsupported_kind_errors_without_raising(app, db, clean_user):
    """`create_report` only ever writes a `kind` from `VALID_KINDS`
    ("topic"/"author_group", both runnable now) — this exercises
    `run_report`'s own defensive check for a `kind` outside
    `_RUNNABLE_KINDS` by constructing the row directly, bypassing
    `create_report` the same way this test always has."""
    with app.app_context():
        report = Report(
            user_id=clean_user.id,
            kind="not_a_real_kind",
            title="Mystery report",
            params={},
            status="pending",
        )
        db.session.add(report)
        db.session.commit()

        result = report_service.run_report(report)
        assert result["status"] == "error"
        assert report.status == "error"
        assert report.error == "UnsupportedReportKind"


# ----------------------------------------------------------------------------
# run_report — persistence: upsert_paper yes, link_user_paper never
# ----------------------------------------------------------------------------


def test_run_report_upserts_papers_but_never_links_them(app, db, clean_user, monkeypatch):
    """Critical regression guard: a report must never dump its (up to 400)
    historical works into the user's Discover feed via link_user_paper."""

    def _boom_link(*a, **kw):
        raise AssertionError("link_user_paper must not be called by report generation")

    monkeypatch.setattr(service, "link_user_paper", _boom_link)

    payloads = [
        _payload("W1", title="Paper One", year=2023),
        _payload("W2", title="Paper Two", year=2024),
    ]
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: payloads)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 2}
        )
        result = report_service.run_report(report)

        assert result["status"] == "ok"
        assert Paper.query.count() == 2
        assert UserPaper.query.count() == 0
        assert report.item_count == 2


def test_run_report_is_idempotent_on_rerun(app, db, clean_user, monkeypatch):
    payloads = [_payload("W-idem", title="Idempotent Paper", year=2024)]
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: payloads)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 1}
        )
        report_service.run_report(report)
        report_service.run_report(report)

        assert Paper.query.count() == 1
        assert Report.query.count() == 1


# ----------------------------------------------------------------------------
# run_report — map phase: one bad chunk doesn't kill the report
# ----------------------------------------------------------------------------


def test_run_report_one_failed_map_chunk_yields_partial_but_keeps_the_rest(
    app, db, clean_user, monkeypatch
):
    payloads = [
        _payload("Y1", title="Old Paper", year=2020),
        _payload("Y2", title="New Paper", year=2024),
    ]
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: payloads)

    calls = {"n": 0, "contexts": []}

    def _fake_chunk(items, *, context, user=None):
        calls["n"] += 1
        calls["contexts"].append(context)
        if calls["n"] == 1:
            return None
        return _FAKE_CHUNK_SUMMARY

    monkeypatch.setattr(ai_service, "summarize_report_chunk", _fake_chunk)

    seen_chunk_summaries = {}

    def _fake_synthesize(chunk_summaries, stats, *, user=None):
        seen_chunk_summaries["value"] = chunk_summaries
        return dict(_FAKE_SECTIONS)

    monkeypatch.setattr(ai_service, "synthesize_report", _fake_synthesize)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 5}
        )
        result = report_service.run_report(report)

        assert calls["n"] == 2  # both year-groups' chunks were attempted
        assert seen_chunk_summaries["value"] == [_FAKE_CHUNK_SUMMARY]  # the failed one was dropped
        assert result["status"] == "partial"
        assert report.status == "partial"
        assert report.sections == _FAKE_SECTIONS  # reduce still ran on the surviving chunk
        assert report.stats["total"] > 0  # stats backbone is untouched by the map failure


# ----------------------------------------------------------------------------
# run_report — reduce phase: synthesize_report(None) still shows stats
# ----------------------------------------------------------------------------


def test_run_report_synthesize_none_keeps_stats_but_marks_partial(app, db, clean_user, monkeypatch):
    payloads = [_payload("Z1", title="Some Paper", year=2024)]
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: payloads)
    monkeypatch.setattr(ai_service, "synthesize_report", lambda *a, **kw: None)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 1}
        )
        result = report_service.run_report(report)

        assert result["status"] == "partial"
        assert report.status == "partial"
        assert report.sections == {}
        assert report.stats  # non-empty — the numeric backbone still rendered
        assert report.stats["year_series"]


# ----------------------------------------------------------------------------
# run_report — AI fully disabled: still a usable, stats-only report
# ----------------------------------------------------------------------------


def test_run_report_ai_disabled_still_produces_a_stats_only_report(
    app, db, clean_user, monkeypatch
):
    """Unlike every other test in this file, this one restores the *real*
    summarize_report_chunk/synthesize_report (the autouse fixture replaces
    both with fakes that don't consult is_ai_enabled at all) so flipping
    is_ai_enabled to False actually exercises the real disabled-AI path
    instead of a fake that would return a summary regardless."""
    monkeypatch.setattr(ai_service, "summarize_report_chunk", _REAL_SUMMARIZE_CHUNK)
    monkeypatch.setattr(ai_service, "synthesize_report", _REAL_SYNTHESIZE_REPORT)
    monkeypatch.setattr(ai_service, "synthesize_author_group_report", _REAL_SYNTHESIZE_GROUP)
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)
    payloads = [_payload("N1", title="No AI Paper", year=2024)]
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: payloads)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 1}
        )
        result = report_service.run_report(report)  # must not raise / 500

        assert result["status"] == "partial"
        assert report.sections == {}
        assert report.stats["year_series"]
        assert report.model_version is None


# ----------------------------------------------------------------------------
# run_report — SourceThrottledError degrades to partial, never escapes
# ----------------------------------------------------------------------------


def test_run_report_source_throttled_becomes_partial_not_an_exception(
    app, db, clean_user, monkeypatch
):
    def _boom(*a, **kw):
        raise SourceThrottledError("openalex rate limit")

    monkeypatch.setattr(oa, "aggregate_works", _boom)
    monkeypatch.setattr(oa, "works_in_range", _boom)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 1}
        )
        result = report_service.run_report(report)  # must not raise

        assert result["status"] == "partial"
        assert report.status == "partial"
        assert report.item_count == 0
        assert report.stats["year_series"] == []


def test_run_report_throttled_mid_stats_keeps_earlier_dimensions(app, db, clean_user, monkeypatch):
    """A rate limit hit on, say, the 3rd aggregate call must not wipe the
    dimensions already fetched before it."""
    calls = {"n": 0}

    def _flaky_aggregate(query, *, since_year, until_year, group_by):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise SourceThrottledError("openalex rate limit")
        return _DEFAULT_AGGREGATE.get(group_by, [])

    monkeypatch.setattr(oa, "aggregate_works", _flaky_aggregate)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 1}
        )
        result = report_service.run_report(report)

        assert result["status"] == "partial"
        assert report.stats["year_series"]  # the one dimension fetched before the throttle survived


# ----------------------------------------------------------------------------
# run_report — an unexpected exception sets status=error and re-raises
# ----------------------------------------------------------------------------


def test_run_report_unexpected_exception_sets_error_and_reraises(app, db, clean_user, monkeypatch):
    def _boom(*a, **kw):
        raise ValueError("something genuinely broke")

    monkeypatch.setattr(oa, "aggregate_works", _boom)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 1}
        )
        with pytest.raises(ValueError):
            report_service.run_report(report)

        assert report.status == "error"
        assert report.error == "ValueError"
        assert report.finished_at is not None


# ----------------------------------------------------------------------------
# Journal quality distribution (Faz 5.3 tie-in)
# ----------------------------------------------------------------------------


def test_run_report_journal_quartiles_reflect_seeded_journals(app, db, clean_user, monkeypatch):
    db.session.add(Journal(issn_l="1234-567X", title="Q1 Journal", sjr_quartile="Q1"))
    db.session.commit()

    payloads = [
        _payload("J1", title="In a seeded journal", year=2024, issn_l="1234-567X"),
        _payload("J2", title="No journal seeded", year=2024, issn_l=None),
    ]
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: payloads)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["ai safety"], "years": 1}
        )
        report_service.run_report(report)

        assert report.stats["journal_quartiles"].get("Q1") == 1
        assert report.stats["journal_quartiles"].get("unknown") == 1


# ----------------------------------------------------------------------------
# list_user_reports / get_report / delete_report
# ----------------------------------------------------------------------------


def test_list_get_delete_report_round_trip(app, db, clean_user):
    with app.app_context():
        r1, _ = report_service.create_report(clean_user, "topic", {"keywords": ["a"], "years": 1})
        r2, _ = report_service.create_report(clean_user, "topic", {"keywords": ["b"], "years": 1})

        rows = report_service.list_user_reports(clean_user)
        assert {r.id for r in rows} == {r1.id, r2.id}

        fetched = report_service.get_report(clean_user, r1.id)
        assert fetched is not None and fetched.id == r1.id

        assert report_service.delete_report(clean_user, r1.id) is True
        assert report_service.get_report(clean_user, r1.id) is None
        assert report_service.delete_report(clean_user, r1.id) is False


def test_get_report_scoped_to_owner(app, db, clean_user):
    with app.app_context():
        other = User(
            username="reportuser_other",
            email="reportuser_other@example.test",
            full_name="Other User",
            password_hash=LocalAuthStrategy.hash_password("x12345678"),
            is_active=True,
        )
        db.session.add(other)
        db.session.commit()
        try:
            r1, _ = report_service.create_report(
                clean_user, "topic", {"keywords": ["a"], "years": 1}
            )
            assert report_service.get_report(other, r1.id) is None
            assert report_service.delete_report(other, r1.id) is False
        finally:
            db.session.query(User).filter_by(id=other.id).delete()
            db.session.commit()


# ----------------------------------------------------------------------------
# author_group reports — create_report validation
# ----------------------------------------------------------------------------


def _make_group_with_members(db, user, member_specs, *, name="Test Group"):
    """Create an `AuthorGroup` for `user` and add one `UserAuthor` per
    `(author_name, openalex_id_or_None)` pair in `member_specs`.

    Builds `UserAuthor` rows directly (no `service.follow_author`/network
    mock needed) — same shortcut `tests/modules/test_author_tracking.py`'s
    `test_paused_and_unresolved_follows_are_skipped` uses for an unresolved
    follow, since all these tests care about is the `openalex_id` value, not
    how it got resolved.
    """
    group, error = service.create_author_group(user, name)
    assert error is None, error
    members = []
    for author_name, openalex_id in member_specs:
        member = UserAuthor(
            user_id=user.id, author_name=author_name, openalex_id=openalex_id, active=False
        )
        db.session.add(member)
        db.session.commit()
        _row, error = service.add_group_member(user, group.id, member.id)
        assert error is None, error
        members.append(member)
    return group, members


def test_create_report_author_group_requires_group_id(app, db, clean_user):
    with app.app_context():
        row, error = report_service.create_report(clean_user, "author_group", {"years": 3})
        assert row is None
        assert str(error) == str(service.GROUP_NOT_FOUND_MESSAGE)


def test_create_report_author_group_rejects_unknown_group(app, db, clean_user):
    with app.app_context():
        row, error = report_service.create_report(
            clean_user, "author_group", {"group_id": 999_999, "years": 3}
        )
        assert row is None
        assert str(error) == str(service.GROUP_NOT_FOUND_MESSAGE)


def test_create_report_author_group_rejects_another_users_group(app, db, clean_user):
    """Ownership is a security boundary here, not just a friendlier error —
    a user must not spend their daily report quota over someone else's
    followed authors."""
    with app.app_context():
        other = User(
            username="reportuser_other_ag",
            email="reportuser_other_ag@example.test",
            full_name="Other Group Owner",
            password_hash=LocalAuthStrategy.hash_password("x12345678"),
            is_active=True,
        )
        db.session.add(other)
        db.session.commit()
        try:
            group, error = service.create_author_group(other, "Not yours")
            assert error is None

            row, error = report_service.create_report(
                clean_user, "author_group", {"group_id": group.id, "years": 3}
            )
            assert row is None
            assert str(error) == str(service.GROUP_NOT_FOUND_MESSAGE)
        finally:
            db.session.execute(
                text("DELETE FROM author_groups WHERE user_id = :uid"), {"uid": other.id}
            )
            db.session.query(User).filter_by(id=other.id).delete()
            db.session.commit()


def test_create_report_author_group_rejects_empty_group(app, db, clean_user):
    with app.app_context():
        group, error = service.create_author_group(clean_user, "Empty group")
        assert error is None

        row, error = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 3}
        )
        assert row is None
        assert str(error) == str(report_service.REPORT_AUTHOR_GROUP_EMPTY_MESSAGE)


def test_create_report_author_group_persists_pending_row(app, db, clean_user):
    with app.app_context():
        group, _members = _make_group_with_members(
            db, clean_user, [("Alice Researcher", "A1")], name="My Team"
        )
        row, error = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 4}
        )
        assert error is None
        assert row is not None
        assert row.status == "pending"
        assert row.kind == "author_group"
        assert row.params["group_id"] == group.id
        assert row.params["years"] == 4
        assert "My Team" in row.title


def test_create_report_author_group_rejects_year_span_too_large(app, db, clean_user):
    with app.app_context():
        group, _members = _make_group_with_members(db, clean_user, [("Alice Researcher", "A1")])
        row, error = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 99}
        )
        assert row is None


# ----------------------------------------------------------------------------
# author_group reports — run_report
# ----------------------------------------------------------------------------


def test_run_report_author_group_basic(app, db, clean_user, monkeypatch):
    group, _members = _make_group_with_members(
        db, clean_user, [("Alice Researcher", "A1"), ("Bob Researcher", "A2")]
    )

    def fake_works(author_id, *, since=None, max_results=25):
        if author_id == "A1":
            return [_payload("W-A1-1", title="Alice Paper", year=2024)]
        return [_payload("W-A2-1", title="Bob Paper", year=2023)]

    monkeypatch.setattr(oa, "works_by_author", fake_works)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 3}
        )
        result = report_service.run_report(report)

        assert result["status"] == "ok"
        assert report.status == "ok"
        assert report.item_count == 2
        assert Paper.query.count() == 2
        assert UserPaper.query.count() == 0
        assert set(report.stats["members"]) == {"Alice Researcher", "Bob Researcher"}
        assert report.stats["members"]["Alice Researcher"]["total"] == 1
        assert report.stats["members"]["Bob Researcher"]["total"] == 1
        assert report.stats["unresolved_members"] == []


def test_run_report_author_group_one_member_fails_others_kept(app, db, clean_user, monkeypatch):
    """Critical isolation guard: one member's fetch blowing up must not sink
    the report — the rest of the group still renders and the run is
    `partial`, not `error`."""
    group, _members = _make_group_with_members(
        db, clean_user, [("Alice Researcher", "A1"), ("Bob Researcher", "A2")]
    )

    def flaky_works(author_id, *, since=None, max_results=25):
        if author_id == "A1":
            raise ValueError("openalex hiccup")
        return [_payload("W-A2-1", title="Bob Paper", year=2023)]

    monkeypatch.setattr(oa, "works_by_author", flaky_works)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 3}
        )
        result = report_service.run_report(report)

        assert result["status"] == "partial"
        assert report.status == "partial"
        assert report.stats["members"]["Alice Researcher"] is None
        assert report.stats["members"]["Bob Researcher"]["total"] == 1
        assert Paper.query.count() == 1


def test_run_report_author_group_throttled_stops_remaining_members(
    app, db, clean_user, monkeypatch
):
    group, _members = _make_group_with_members(
        db, clean_user, [("Alice Researcher", "A1"), ("Bob Researcher", "A2")]
    )

    calls = {"n": 0}

    def always_throttled(author_id, *, since=None, max_results=25):
        calls["n"] += 1
        raise SourceThrottledError("openalex rate limit")

    monkeypatch.setattr(oa, "works_by_author", always_throttled)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 3}
        )
        result = report_service.run_report(report)  # must not raise

        assert result["status"] == "partial"
        # Alice (alphabetically first — list_group_members orders by name)
        # trips the shared throttle flag; Bob's fetch is then skipped
        # without spending a call.
        assert calls["n"] == 1


def test_run_report_author_group_unresolved_member_is_skipped_not_failed(
    app, db, clean_user, monkeypatch
):
    group, _members = _make_group_with_members(
        db, clean_user, [("Alice Researcher", "A1"), ("Ghost Author", None)]
    )
    monkeypatch.setattr(oa, "works_by_author", lambda *a, **kw: [_payload("W1", year=2024)])

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 3}
        )
        result = report_service.run_report(report)

        # An unresolved identity alone must not force `partial` — it is a
        # normal, visible skip, not a failure.
        assert result["status"] == "ok"
        assert report.stats["unresolved_members"] == ["Ghost Author"]
        assert "Ghost Author" not in report.stats["members"]


def test_run_report_author_group_upserts_papers_but_never_links_them(
    app, db, clean_user, monkeypatch
):
    """Same regression guard as the "topic" report's — an author_group run
    must never dump its item set into the user's Discover feed."""

    def _boom_link(*a, **kw):
        raise AssertionError("link_user_paper must not be called by report generation")

    monkeypatch.setattr(service, "link_user_paper", _boom_link)

    group, _members = _make_group_with_members(db, clean_user, [("Alice Researcher", "A1")])
    monkeypatch.setattr(
        oa, "works_by_author", lambda *a, **kw: [_payload("W-link-test", year=2024)]
    )

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 2}
        )
        result = report_service.run_report(report)

        assert result["status"] == "ok"
        assert Paper.query.count() == 1
        assert UserPaper.query.count() == 0


def test_run_report_author_group_keyword_overlap(app, db, clean_user, monkeypatch):
    """The group report's actual payoff: does this group's recent output
    already touch a keyword the user personally follows — simple,
    case-insensitive substring matching, no LLM."""
    _kw, error = add_user_keyword(clean_user, "graph neural networks")
    assert error is None

    group, _members = _make_group_with_members(db, clean_user, [("Alice Researcher", "A1")])
    monkeypatch.setattr(
        oa,
        "works_by_author",
        lambda *a, **kwargs: [
            _payload("W-kw-1", title="A survey of Graph Neural Networks", year=2024),
            _payload("W-kw-2", title="Unrelated topic entirely", year=2024),
        ],
    )

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 2}
        )
        report_service.run_report(report)

        overlap = report.stats["keyword_overlap"]
        assert "graph neural networks" in overlap["matched_keywords"]
        assert overlap["match_count"] == 1
        assert overlap["matches"][0]["title"] == "A survey of Graph Neural Networks"


def test_run_report_author_group_detects_coauthorship(app, db, clean_user, monkeypatch):
    """Two members' own `works_by_author` results sharing the same paper
    identity (same source/external_id) is treated as OpenAlex crediting both
    of them on it — a direct collaboration signal."""
    group, _members = _make_group_with_members(
        db, clean_user, [("Alice Researcher", "A1"), ("Bob Researcher", "A2")]
    )
    shared = _payload("W-shared", title="Joint Paper", year=2024)

    def fake_works(author_id, *, since=None, max_results=25):
        if author_id == "A1":
            return [shared]
        return [shared, _payload("W-bob-solo", title="Bob Solo Paper", year=2024)]

    monkeypatch.setattr(oa, "works_by_author", fake_works)

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 2}
        )
        report_service.run_report(report)

        links = report.stats["coauthorship"]
        assert len(links) == 1
        assert set(links[0]["members"]) == {"Alice Researcher", "Bob Researcher"}
        assert links[0]["count"] == 1


def test_run_report_author_group_ai_disabled_still_produces_a_stats_only_report(
    app, db, clean_user, monkeypatch
):
    """Unlike every other test in this section, this one restores the *real*
    summarize_report_chunk/synthesize_report (see the "topic" report's
    equivalent test for why) so flipping is_ai_enabled to False actually
    exercises the real disabled-AI path."""
    monkeypatch.setattr(ai_service, "summarize_report_chunk", _REAL_SUMMARIZE_CHUNK)
    monkeypatch.setattr(ai_service, "synthesize_report", _REAL_SYNTHESIZE_REPORT)
    monkeypatch.setattr(ai_service, "synthesize_author_group_report", _REAL_SYNTHESIZE_GROUP)
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)

    group, _members = _make_group_with_members(db, clean_user, [("Alice Researcher", "A1")])
    monkeypatch.setattr(oa, "works_by_author", lambda *a, **kw: [_payload("W-noai", year=2024)])

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 2}
        )
        result = report_service.run_report(report)  # must not raise / 500

        assert result["status"] == "partial"
        assert report.sections == {}
        assert report.stats["members"]["Alice Researcher"]["total"] == 1
        assert report.model_version is None


def test_run_report_author_group_sections_shape(app, db, clean_user, monkeypatch):
    group, _members = _make_group_with_members(db, clean_user, [("Alice Researcher", "A1")])
    monkeypatch.setattr(oa, "works_by_author", lambda *a, **kw: [_payload("W-shape", year=2024)])

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "author_group", {"group_id": group.id, "years": 2}
        )
        report_service.run_report(report)

        sections = report.sections
        assert set(sections) == {
            "tldr",
            "members",
            "shared_topics",
            "overlap_with_you",
            "coauthorship_notes",
        }
        member = sections["members"][0]
        assert member["name"] == "Alice Researcher"
        # `focus` is the model's own characterisation of the person's work,
        # not a join of map-phase theme fragments — that split is the whole
        # reason the dossier has its own reduce prompt instead of reusing the
        # topic report's time-shaped one.
        assert member["focus"] == "Alice Researcher şunu çalışıyor."
        assert member["recent_highlights"] == ["not"]
        # Countable things stay computed here rather than being restated by
        # the model: venues/topics come from `stats`, never from `synth`.
        assert "venues" in member and "topics" in member
        assert sections["overlap_with_you"] == "Senin anahtar kelimelerinle kesişiyor."


def test_oa_share_reads_openalex_own_boolean_shape(app, db, clean_user, monkeypatch):
    """`open_access.is_oa` comes back keyed "1"/"0" with "true"/"false" in the
    display name, not keyed "true".

    Matching on the key alone made every report report 0% open access, and
    no test caught it because the fixture had invented the friendlier shape.
    This pins the real one, and the raw-key spelling too, so neither an
    upstream change nor a normalisation change here can silently zero the
    figure again.
    """
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: [])

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["oa"], "years": 2}
        )
        report_service.run_report(report)
        # 6 of 8 works are open access in _DEFAULT_AGGREGATE.
        assert report.stats["oa_share"] == 0.75


def test_oa_share_is_none_rather_than_zero_when_unknown(app, db, clean_user, monkeypatch):
    """No OA data at all must read as "unknown", not as "0% open access" —
    a fabricated zero is worse than an absent figure."""
    aggregates = dict(_DEFAULT_AGGREGATE)
    aggregates["open_access.is_oa"] = []
    monkeypatch.setattr(oa, "aggregate_works", lambda q, **kw: aggregates.get(kw["group_by"], []))
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: [])

    with app.app_context():
        report, _err = report_service.create_report(
            clean_user, "topic", {"keywords": ["oa"], "years": 2}
        )
        report_service.run_report(report)
        assert report.stats["oa_share"] is None
