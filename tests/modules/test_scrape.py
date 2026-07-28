"""Scrape module — unit tests + a stubbed end-to-end through the Celery task.

We never hit real source APIs in tests. `enabled_sources` is monkey-patched
in the service to a single fake adapter returning a fixed payload list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.models import IdentifierType, Keyword, UserKeyword
from app.modules.scrape.models import Paper, UserPaper
from app.modules.scrape.service import (
    link_user_paper,
    list_user_papers,
    scrape_for_user,
    upsert_paper,
)
from app.modules.scrape.sources.payload import PaperPayload


def _fake_sources(payloads):
    """A single fake source module exposing search_for_keywords."""
    fake = SimpleNamespace(
        SOURCE_NAME="fake",
        search_for_keywords=lambda keywords, *, max_results=25: payloads,
    )
    return lambda: {"fake": fake}


def _payload(ext_id: str, *, title="A Title", keyword="transformer") -> PaperPayload:
    return PaperPayload(
        source="arxiv",
        external_id=ext_id,
        title=title,
        abstract="abs",
        authors=["A. One", "B. Two"],
        url=f"http://arxiv.org/abs/{ext_id}",
        pdf_url=f"http://arxiv.org/pdf/{ext_id}",
        published_at=datetime(2026, 1, 15, tzinfo=UTC),
        categories=["cs.LG", "stat.ML"],
    )


@pytest.fixture
def clean(db):
    db.session.execute(text("DELETE FROM notifications"))
    db.session.execute(text("DELETE FROM paper_chat_messages"))
    db.session.execute(text("DELETE FROM paper_notes"))
    db.session.execute(text("DELETE FROM user_papers"))
    db.session.execute(text("DELETE FROM papers"))
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
    db.session.execute(text("DELETE FROM user_sources"))
    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()
    db.session.add(
        IdentifierType(
            code="email",
            name="Email",
            validation_regex=r"^[^@]+@[^@]+\.[^@]+$",
            verification_method="email_link",
        )
    )
    db.session.commit()
    u = User(
        username="alice",
        email="alice@ex.com",
        full_name="Alice",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(text("DELETE FROM notifications"))
    db.session.execute(text("DELETE FROM paper_chat_messages"))
    db.session.execute(text("DELETE FROM paper_notes"))
    db.session.execute(text("DELETE FROM user_papers"))
    db.session.execute(text("DELETE FROM papers"))
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
    db.session.execute(text("DELETE FROM user_sources"))
    db.session.execute(text("DELETE FROM scan_runs"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.query(User).delete()
    db.session.commit()


def test_upsert_paper_is_idempotent(db, clean):
    p1 = upsert_paper(_payload("2401.00001"))
    p2 = upsert_paper(_payload("2401.00001"))
    assert p1.id == p2.id
    assert Paper.query.count() == 1


def test_link_user_paper_dedupes(db, clean):
    paper = upsert_paper(_payload("2401.00002"))
    _, c1 = link_user_paper(clean, paper, matched_keyword="x")
    _, c2 = link_user_paper(clean, paper, matched_keyword="x")
    assert c1 is True
    assert c2 is False
    assert UserPaper.query.filter_by(user_id=clean.id).count() == 1


def test_scrape_skips_when_no_keywords(db, clean):
    result = scrape_for_user(clean)
    assert result == {"hits": 0, "linked": 0, "reason": "no_keywords"}


def test_scrape_persists_and_links(db, clean, monkeypatch):
    # Give the user one keyword
    kw = Keyword(value="transformer architectures")
    db.session.add(kw)
    db.session.commit()
    db.session.add(UserKeyword(user_id=clean.id, keyword_id=kw.id))
    db.session.commit()

    # Stub the source registry: one fake source, two results
    monkeypatch.setattr(
        "app.modules.scrape.service.enabled_sources",
        _fake_sources(
            [
                _payload("2401.10001", title="On transformer architectures for vision"),
                _payload("2401.10002", title="A new optimizer"),
            ]
        ),
    )

    result = scrape_for_user(clean)
    assert result["hits"] == 2
    assert result["linked"] == 2
    assert result["sources"] == {"fake": 2}

    rows = list_user_papers(clean)
    assert len(rows) == 2
    # Title-matched row gets the right keyword; the other falls back to first kw
    matched = {r.paper.external_id: r.matched_keyword for r in rows}
    assert matched["2401.10001"] == "transformer architectures"
    # Second paper title doesn't contain the kw → falls back to first keyword
    assert matched["2401.10002"] == "transformer architectures"


def test_scrape_run_idempotent(db, clean, monkeypatch):
    kw = Keyword(value="rl")
    db.session.add(kw)
    db.session.commit()
    db.session.add(UserKeyword(user_id=clean.id, keyword_id=kw.id))
    db.session.commit()

    monkeypatch.setattr(
        "app.modules.scrape.service.enabled_sources",
        _fake_sources([_payload("2401.55555", title="An RL paper")]),
    )

    r1 = scrape_for_user(clean)
    r2 = scrape_for_user(clean)
    assert r1["linked"] == 1
    assert r2["linked"] == 0  # no new links second time around
    assert Paper.query.count() == 1
    assert UserPaper.query.count() == 1


def test_scrape_isolates_failing_source(db, clean, monkeypatch):
    """One source raising must not kill the run — the healthy source still
    lands and the failed one is marked with -1 in the summary."""
    kw = Keyword(value="rl")
    db.session.add(kw)
    db.session.commit()
    db.session.add(UserKeyword(user_id=clean.id, keyword_id=kw.id))
    db.session.commit()

    def _boom(keywords, *, max_results=25):
        raise RuntimeError("rate limited")

    broken = SimpleNamespace(SOURCE_NAME="broken", search_for_keywords=_boom)
    healthy = SimpleNamespace(
        SOURCE_NAME="healthy",
        search_for_keywords=lambda keywords, *, max_results=25: [_payload("2401.88888")],
    )
    monkeypatch.setattr(
        "app.modules.scrape.service.enabled_sources",
        lambda: {"broken": broken, "healthy": healthy},
    )

    result = scrape_for_user(clean)
    assert result["linked"] == 1
    assert result["sources"] == {"broken": -1, "healthy": 1}


def test_celery_task_through_eager(db, clean, monkeypatch):
    """run_for_user task in eager mode -> calls our service end-to-end."""
    from app.tasks.scrape_tasks import run_for_user

    kw = Keyword(value="diffusion")
    db.session.add(kw)
    db.session.commit()
    db.session.add(UserKeyword(user_id=clean.id, keyword_id=kw.id))
    db.session.commit()

    monkeypatch.setattr(
        "app.modules.scrape.service.enabled_sources",
        _fake_sources([_payload("2401.77777")]),
    )
    result = run_for_user.delay(clean.id).get()
    assert result["linked"] == 1


def test_manual_run_also_refreshes_rss_feeds(auth_client, monkeypatch):
    """ "Scrape now" must queue the feed pipeline too.

    RSS lives in a separate pipeline (global ingest + per-user relevance
    linking) that only Beat used to drive, so a manual scan refreshed the
    academic sources and silently left the news feeds at last night's state.
    """
    from app.tasks import feed_tasks, scrape_tasks

    queued: dict[str, tuple] = {}
    monkeypatch.setattr(
        scrape_tasks.run_for_user,
        "delay",
        lambda *a, **kw: queued.setdefault("scrape", (a, kw)) or SimpleNamespace(id="s1"),
    )
    monkeypatch.setattr(
        feed_tasks.link_for_user,
        "delay",
        lambda *a, **kw: queued.setdefault("feeds", (a, kw)) or SimpleNamespace(id="f1"),
    )

    client, uid = auth_client
    r = client.post("/papers/run", follow_redirects=False)
    assert r.status_code in (200, 302)
    assert queued["scrape"][1]["trigger"] == "manual"
    assert queued["feeds"][0] == (uid,)


def test_manual_run_survives_a_feed_task_that_cannot_be_queued(auth_client, monkeypatch):
    """The scrape is already away by then — a broker hiccup on the second
    enqueue must not turn the user's button press into a 500."""
    from app.tasks import feed_tasks, scrape_tasks

    def _boom(*_a, **_kw):
        raise RuntimeError("broker down")

    monkeypatch.setattr(
        scrape_tasks.run_for_user, "delay", lambda *a, **kw: SimpleNamespace(id="s1")
    )
    monkeypatch.setattr(feed_tasks.link_for_user, "delay", _boom)

    client, _uid = auth_client
    r = client.post("/papers/run", follow_redirects=False)
    assert r.status_code in (200, 302)


def test_feed_route_requires_login(client):
    r = client.get("/papers/", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_scrape_status_poll_requires_login(client):
    r = client.get("/papers/status/some-task-id", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_read_later_toggling(db, clean):
    from app.modules.scrape.service import toggle_read_later

    paper = upsert_paper(_payload("2401.00003"))
    link, _ = link_user_paper(clean, paper, matched_keyword="x")
    assert link.read_later is False

    new_status = toggle_read_later(link)
    assert new_status is True
    assert link.read_later is True

    new_status = toggle_read_later(link)
    assert new_status is False
    assert link.read_later is False


def test_user_enabled_sources_defaults_to_all(db, clean, monkeypatch):
    """A user with no UserSource rows scans every enabled source (opt-out)."""
    from app.modules.scrape.service import user_enabled_sources

    a = SimpleNamespace(SOURCE_NAME="a")
    b = SimpleNamespace(SOURCE_NAME="b")
    monkeypatch.setattr("app.modules.scrape.service.enabled_sources", lambda: {"a": a, "b": b})
    assert set(user_enabled_sources(clean).keys()) == {"a", "b"}


def test_set_user_source_mutes_and_reenables(db, clean, monkeypatch):
    from app.modules.scrape.service import (
        list_user_source_prefs,
        set_user_source,
        user_enabled_sources,
    )

    a = SimpleNamespace(SOURCE_NAME="a")
    b = SimpleNamespace(SOURCE_NAME="b")
    monkeypatch.setattr("app.modules.scrape.service.enabled_sources", lambda: {"a": a, "b": b})

    # Mute "b"
    set_user_source(clean, "b", False)
    assert list_user_source_prefs(clean) == {"b": False}
    assert set(user_enabled_sources(clean).keys()) == {"a"}

    # Re-enable "b" (idempotent upsert — no duplicate row)
    set_user_source(clean, "b", True)
    assert list_user_source_prefs(clean) == {"b": True}
    assert set(user_enabled_sources(clean).keys()) == {"a", "b"}
    from app.modules.scrape.models import UserSource

    assert UserSource.query.filter_by(user_id=clean.id, source_name="b").count() == 1


def test_scrape_respects_muted_source(db, clean, monkeypatch):
    """A muted source is not queried during a scrape run."""
    from app.modules.scrape.service import set_user_source

    kw = Keyword(value="rl")
    db.session.add(kw)
    db.session.commit()
    db.session.add(UserKeyword(user_id=clean.id, keyword_id=kw.id))
    db.session.commit()

    healthy = SimpleNamespace(
        SOURCE_NAME="healthy",
        search_for_keywords=lambda keywords, *, max_results=25: [_payload("2401.99991")],
    )
    muted = SimpleNamespace(
        SOURCE_NAME="muted",
        search_for_keywords=lambda keywords, *, max_results=25: [_payload("2401.99992")],
    )
    monkeypatch.setattr(
        "app.modules.scrape.service.enabled_sources",
        lambda: {"healthy": healthy, "muted": muted},
    )

    set_user_source(clean, "muted", False)
    result = scrape_for_user(clean)
    assert result["sources"] == {"healthy": 1}  # muted source absent


def test_notifications_creation(db, clean):
    from app.core.models.notification import Notification, add_notification

    noti = add_notification(clean.id, "Test Title", "Test Message")
    assert noti.id is not None
    assert noti.title == "Test Title"
    assert noti.message == "Test Message"
    assert noti.is_read is False

    fetched = Notification.query.filter_by(user_id=clean.id).first()
    assert fetched is not None
    assert fetched.id == noti.id
