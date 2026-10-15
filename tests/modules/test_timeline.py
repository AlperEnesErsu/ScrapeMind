"""Characterization tests for the library activity timeline (`build_timeline`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.service import add_user_keyword
from app.modules.scrape.models import Paper, ScanRun
from app.modules.scrape.service import (
    add_note,
    build_timeline,
    link_user_paper,
    set_dismissed,
    set_favorite,
    upsert_paper,
)
from app.modules.scrape.sources.payload import PaperPayload

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _wipe(db):
    """Remove the fixture user and everything hanging off it (FK order)."""
    uid = "(SELECT id FROM users WHERE username = 'timelineuser')"
    for stmt in (
        f"DELETE FROM paper_notes WHERE user_paper_id IN (SELECT id FROM user_papers WHERE user_id IN {uid})",
        f"DELETE FROM user_papers WHERE user_id IN {uid}",
        f"DELETE FROM user_keywords WHERE user_id IN {uid}",
        f"DELETE FROM scan_runs WHERE user_id IN {uid}",
        "DELETE FROM papers WHERE source = 'timeline_test'",
        "DELETE FROM users WHERE username = 'timelineuser'",
    ):
        db.session.execute(text(stmt))
    db.session.commit()


@pytest.fixture
def tl_user(db):
    _wipe(db)
    u = User(
        username="timelineuser",
        email="timelineuser@example.test",
        full_name="Timeline User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.rollback()
    _wipe(db)


def _paper(ext_id: str, title: str) -> Paper:
    return upsert_paper(
        PaperPayload(
            source="timeline_test",
            external_id=ext_id,
            title=title,
            abstract="",
            authors=[],
            url=f"https://example.test/{ext_id}",
            pdf_url=None,
            published_at=NOW,
            categories=[],
        )
    )


def _run(db, user, kind, trigger, minutes_ago, *, status="ok", new_items=0):
    started = NOW - timedelta(minutes=minutes_ago)
    db.session.add(
        ScanRun(
            user_id=user.id,
            kind=kind,
            trigger=trigger,
            status=status,
            started_at=started,
            finished_at=started + timedelta(seconds=30),
            new_items=new_items,
        )
    )
    db.session.commit()


def test_scan_runs_become_events_with_kind_specific_titles(db, tl_user):
    _run(db, tl_user, "scrape", "manual", 10, new_items=5)
    _run(db, tl_user, "scrape", "auto", 20)
    _run(db, tl_user, "patents", "manual", 30, new_items=2)
    _run(db, tl_user, "patents", "auto", 40)
    _run(db, tl_user, "authors", "auto", 50, status="partial", new_items=1)
    _run(db, tl_user, "scrape", "auto", 60, status="running")  # unfinished: excluded
    _run(db, tl_user, "alerts", "auto", 70)  # unknown kind: excluded

    events = build_timeline(tl_user)

    rows = [(e.kind, e.title, e.badge) for e in events]
    assert rows == [
        ("scrape_run", "Manuel tarama", "+5"),
        ("scrape_run", "Otomatik tarama", None),
        ("patent_run", "Manuel patent taraması", "+2"),
        ("patent_run", "Patent taraması", None),
        ("author_run", "Takip edilen yazarlar", "+1"),
    ]
    assert events[0].when == NOW - timedelta(minutes=10) + timedelta(seconds=30)


def test_paper_events_notes_favorites_dismissals_and_keywords(db, tl_user):
    liked = link_user_paper(tl_user, _paper("t1", "Liked paper"), matched_keyword="quantum")[0]
    hidden = link_user_paper(tl_user, _paper("t2", "Hidden paper"), matched_keyword="noise")[0]
    noted = link_user_paper(tl_user, _paper("t3", "Noted paper"), matched_keyword=None)[0]
    set_favorite(liked, True)
    set_dismissed(hidden, True)
    add_note(noted, "line one\nline two", tag="soru")
    add_user_keyword(tl_user, "graph neural networks")

    by_kind = {e.kind: e for e in build_timeline(tl_user)}

    fav = by_kind["favorited"]
    assert (fav.title, fav.detail, fav.link_url) == (
        "Liked paper",
        "quantum",
        f"/papers/{liked.id}",
    )
    dis = by_kind["dismissed"]
    assert (dis.title, dis.detail, dis.link_url) == (
        "Hidden paper",
        "noise",
        f"/papers/{hidden.id}",
    )
    note = by_kind["note_added"]
    assert (note.title, note.detail, note.tag) == ("Noted paper", "line one line two", "soru")
    assert note.link_url == f"/papers/{noted.id}"
    kw = by_kind["keyword_added"]
    assert kw.title == "graph neural networks"
    assert kw.link_url == "/papers/?q=graph%20neural%20networks"


def test_note_preview_is_truncated_to_one_short_line(db, tl_user):
    link = link_user_paper(tl_user, _paper("t4", "Long note paper"), matched_keyword=None)[0]
    add_note(link, "x" * 200)
    (event,) = [e for e in build_timeline(tl_user) if e.kind == "note_added"]
    assert event.detail == "x" * 78 + "…"


def test_events_are_newest_first_and_capped_at_limit(db, tl_user):
    for i in range(6):
        _run(db, tl_user, "scrape", "auto", i * 5)
    events = build_timeline(tl_user, limit=4)
    assert len(events) == 4
    assert [e.when for e in events] == sorted((e.when for e in events), reverse=True)
