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
    db.session.execute(text("DELETE FROM user_feeds"))
    db.session.execute(text("DELETE FROM user_channels"))
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


def test_manual_run_also_ingests_youtube_channels(auth_client, monkeypatch):
    """Subscribed channels are a third pipeline, and they had the same bug the
    RSS feeds did: "Scrape now" left them at last night's state, so a user who
    had just subscribed pressed scrape, saw nothing, and had no way to tell
    whether the channel had been checked at all.
    """
    from app.tasks import channel_tasks, feed_tasks, scrape_tasks

    queued: dict[str, tuple] = {}
    monkeypatch.setattr(
        scrape_tasks.run_for_user, "delay", lambda *a, **kw: SimpleNamespace(id="s1")
    )
    monkeypatch.setattr(
        feed_tasks.link_for_user, "delay", lambda *a, **kw: SimpleNamespace(id="f1")
    )
    monkeypatch.setattr(
        channel_tasks.ingest_for_user,
        "delay",
        lambda *a, **kw: queued.setdefault("channels", (a, kw)) or SimpleNamespace(id="c1"),
    )

    client, uid = auth_client
    r = client.post("/papers/run", follow_redirects=False)
    assert r.status_code in (200, 302)
    assert queued["channels"][0] == (uid,)
    assert queued["channels"][1]["trigger"] == "manual"


@pytest.mark.parametrize("broken", ["feeds", "channels"])
def test_manual_run_survives_a_secondary_task_that_cannot_be_queued(
    auth_client, monkeypatch, broken
):
    """The scrape is already away by then — a broker hiccup on either of the
    follow-up enqueues must not turn the user's button press into a 500."""
    from app.tasks import channel_tasks, feed_tasks, scrape_tasks

    def _boom(*_a, **_kw):
        raise RuntimeError("broker down")

    monkeypatch.setattr(
        scrape_tasks.run_for_user, "delay", lambda *a, **kw: SimpleNamespace(id="s1")
    )
    monkeypatch.setattr(
        feed_tasks.link_for_user,
        "delay",
        _boom if broken == "feeds" else (lambda *a, **kw: SimpleNamespace(id="f1")),
    )
    monkeypatch.setattr(
        channel_tasks.ingest_for_user,
        "delay",
        _boom if broken == "channels" else (lambda *a, **kw: SimpleNamespace(id="c1")),
    )

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


def test_upsert_paper_dedupes_by_doi(db, clean):
    p1 = upsert_paper(
        {
            "source": "arxiv",
            "external_id": "2401.00001",
            "title": "Paper 1",
            "authors": ["A. One"],
            "categories": ["cs.AI"],
            "doi": "10.1000/182",
        }
    )
    p2 = upsert_paper(
        {
            "source": "pubmed",
            "external_id": "38123456",
            "title": "Paper 1 (PubMed variant)",
            "authors": ["A. One"],
            "categories": ["cs.AI"],
            "doi": "10.1000/182",
        }
    )
    assert p1.id == p2.id
    assert Paper.query.count() == 1


def test_upsert_paper_dedupes_by_doi_across_three_written_forms(db, clean):
    """The same DOI spelled three different ways — bare, doi.org URL, and
    'doi:' scheme — across three different (source, external_id) pairs must
    all normalize to one canonical row."""
    p1 = upsert_paper(
        {
            "source": "arxiv",
            "external_id": "2401.00099",
            "title": "Three Forms",
            "authors": [],
            "categories": [],
            "doi": "https://doi.org/10.9999/xyz",
        }
    )
    p2 = upsert_paper(
        {
            "source": "pubmed",
            "external_id": "38199999",
            "title": "Three Forms (PubMed)",
            "authors": [],
            "categories": [],
            "doi": "doi:10.9999/XYZ",
        }
    )
    p3 = upsert_paper(
        {
            "source": "semantic_scholar",
            "external_id": "abc999",
            "title": "Three Forms (S2)",
            "authors": [],
            "categories": [],
            "doi": "10.9999/xyz",
        }
    )
    assert p1.id == p2.id == p3.id
    assert Paper.query.count() == 1
    assert p1.doi == "10.9999/xyz"


def test_upsert_paper_unparseable_doi_does_not_collapse_distinct_papers(db, clean):
    """An unparseable DOI is stored as None, not as the literal garbage
    string — two unrelated papers that both fail to parse a DOI must NOT be
    treated as the same row just because they share a None doi."""
    p1 = upsert_paper(
        {
            "source": "arxiv",
            "external_id": "2401.00100",
            "title": "First",
            "authors": [],
            "categories": [],
            "doi": "n/a",
        }
    )
    p2 = upsert_paper(
        {
            "source": "pubmed",
            "external_id": "38100000",
            "title": "Second",
            "authors": [],
            "categories": [],
            "doi": "n/a",
        }
    )
    assert p1.doi is None
    assert p2.doi is None
    assert p1.id != p2.id
    assert Paper.query.count() == 2


def test_upsert_paper_enriches_empty_abstract(db, clean):
    """An empty abstract on the existing row gets filled by a second payload
    (a different source, same paper via DOI) that carries one."""
    p1 = upsert_paper(
        {
            "source": "arxiv",
            "external_id": "2401.00200",
            "title": "Enrich Me",
            "abstract": None,
            "authors": [],
            "categories": [],
            "doi": "10.5000/enrich",
        }
    )
    assert p1.abstract is None

    p2 = upsert_paper(
        {
            "source": "semantic_scholar",
            "external_id": "enrich-s2",
            "title": "Enrich Me (S2)",
            "abstract": "Now with an abstract.",
            "authors": [],
            "categories": [],
            "doi": "10.5000/enrich",
        }
    )
    assert p1.id == p2.id
    assert Paper.query.count() == 1
    assert Paper.query.get(p1.id).abstract == "Now with an abstract."


def test_upsert_paper_does_not_overwrite_populated_abstract(db, clean):
    """A populated abstract is never overwritten by a different incoming
    abstract, even on a DOI match."""
    p1 = upsert_paper(
        {
            "source": "arxiv",
            "external_id": "2401.00201",
            "title": "Already Has One",
            "abstract": "Original abstract.",
            "authors": [],
            "categories": [],
            "doi": "10.5000/keep",
        }
    )
    p2 = upsert_paper(
        {
            "source": "semantic_scholar",
            "external_id": "keep-s2",
            "title": "Already Has One (S2)",
            "abstract": "A different abstract that should be ignored.",
            "authors": [],
            "categories": [],
            "doi": "10.5000/keep",
        }
    )
    assert p1.id == p2.id
    assert Paper.query.get(p1.id).abstract == "Original abstract."


def test_upsert_paper_no_enrichment_needed_is_a_clean_no_op(db, clean):
    """Upserting the exact same payload twice must not spuriously touch the
    row: nothing is empty on the existing row, so _enrich finds nothing to
    fill and the second call must not bump updated_at."""
    payload = {
        "source": "arxiv",
        "external_id": "2401.00202",
        "title": "Stable",
        "abstract": "Stable abstract.",
        "authors": ["A. One"],
        "categories": ["cs.AI"],
        "doi": "10.5000/stable",
    }
    p1 = upsert_paper(payload)
    first_updated_at = p1.updated_at
    p2 = upsert_paper(dict(payload))
    assert p1.id == p2.id
    assert Paper.query.count() == 1
    assert Paper.query.get(p1.id).updated_at == first_updated_at


def test_add_user_feed_stores_etag_and_last_modified(db, clean, monkeypatch):
    from app.modules.scrape.service import add_user_feed
    from app.modules.scrape.sources.rss_source import FeedFetchResult

    fake_res = FeedFetchResult(
        payloads=[_payload("guid-1")],
        status="ok",
        etag='"12345"',
        last_modified="Wed, 21 Oct 2025 07:28:00 GMT",
        http_status=200,
        title="Fake Feed",
    )
    monkeypatch.setattr(
        "app.modules.scrape.service.fetch_feed_conditional",
        lambda feed_dict: fake_res,
    )
    feed, err = add_user_feed(clean, "https://example.com/feed.xml")
    assert err is None
    assert feed.etag == '"12345"'
    assert feed.last_modified == "Wed, 21 Oct 2025 07:28:00 GMT"


# ----------------------------------------------------------------------------
# Custom user YouTube channels — the twin of the custom-feed tests above.
# `resolve_channel` is monkeypatched at its source module, not at
# service.py, because add_user_channel imports it locally inside the
# function (the same import-inside-function style `ingest_user_feeds` uses
# for `fetch_feed`), so there is no `service.resolve_channel` name to patch.
# ----------------------------------------------------------------------------

_RESOLVE_CHANNEL_TARGET = "app.modules.scrape.sources.youtube_channel_source.resolve_channel"


def _resolved(channel_id="UC1234567890123456789012", title="Fake Channel"):
    return {
        "channel_id": channel_id,
        "title": title,
        "url": f"https://www.youtube.com/channel/{channel_id}",
    }


def test_add_user_channel_happy_path(db, clean, monkeypatch):
    from app.modules.scrape.service import add_user_channel

    monkeypatch.setattr(_RESOLVE_CHANNEL_TARGET, lambda raw: (_resolved(), None))

    channel, err = add_user_channel(clean, "@somechannel")
    assert err is None
    assert channel is not None
    assert channel.channel_id == "UC1234567890123456789012"
    assert channel.title == "Fake Channel"
    assert channel.active is True


def test_add_user_channel_resolution_failure_creates_no_row(db, clean, monkeypatch):
    from app.modules.scrape.models import UserChannel
    from app.modules.scrape.service import add_user_channel

    monkeypatch.setattr(
        _RESOLVE_CHANNEL_TARGET,
        lambda raw: (None, "Could not find that YouTube channel — check the link and try again."),
    )

    channel, err = add_user_channel(clean, "https://www.youtube.com/@doesnotexist")
    assert channel is None
    assert err == "Could not find that YouTube channel — check the link and try again."
    assert UserChannel.query.filter_by(user_id=clean.id).count() == 0


def test_add_user_channel_reactivates_without_consuming_cap_slot(db, clean, monkeypatch, app):
    from app.modules.scrape.service import (
        add_user_channel,
        remove_user_channel,
        toggle_user_channel,
    )

    monkeypatch.setattr(_RESOLVE_CHANNEL_TARGET, lambda raw: (_resolved(), None))
    monkeypatch.setitem(app.config, "MAX_USER_CHANNELS", 1)

    channel, err = add_user_channel(clean, "@somechannel")
    assert err is None
    toggle_user_channel(clean, channel.id)  # pause it
    assert channel.active is False

    # Re-adding the identical channel while already at the cap must still
    # succeed — it reactivates the existing row rather than creating a new
    # one, so it never touches the cap check.
    channel2, err2 = add_user_channel(clean, "@somechannel")
    assert err2 is None
    assert channel2.id == channel.id
    assert channel2.active is True

    ok = remove_user_channel(clean, channel.id)
    assert ok is True


def test_add_user_channel_enforces_cap_via_system_setting(db, clean, monkeypatch, app):
    from sqlalchemy import text as sa_text

    from app.core.settings.service import set_system_setting
    from app.modules.scrape.service import add_user_channel

    monkeypatch.setattr(
        _RESOLVE_CHANNEL_TARGET,
        lambda raw: (_resolved(channel_id="UC" + raw[-24:].rjust(24, "0")), None),
    )
    try:
        # Admin lowers the cap to 1 at runtime via the system-settings row —
        # this is the proof the admin knob actually governs add_user_channel,
        # not just current_app.config.
        set_system_setting("max_user_channels", 1)

        channel1, err1 = add_user_channel(clean, "@first")
        assert err1 is None
        assert channel1 is not None

        channel2, err2 = add_user_channel(clean, "@second")
        assert channel2 is None
        from app.modules.scrape.service import CHANNEL_CAP_MESSAGE

        assert err2 == CHANNEL_CAP_MESSAGE
    finally:
        db.session.execute(sa_text("DELETE FROM system_settings WHERE key = 'max_user_channels'"))
        db.session.commit()


def test_max_user_channels_survives_garbage_db_value(db, clean, monkeypatch, app):
    from sqlalchemy import text as sa_text

    from app.core.settings.service import set_system_setting
    from app.modules.scrape.service import max_user_channels

    monkeypatch.setitem(app.config, "MAX_USER_CHANNELS", 7)
    try:
        set_system_setting("max_user_channels", "abc")
        assert max_user_channels() == 7

        set_system_setting("max_user_channels", None)
        assert max_user_channels() == 7

        set_system_setting("max_user_channels", -5)
        assert max_user_channels() == 0
    finally:
        db.session.execute(sa_text("DELETE FROM system_settings WHERE key = 'max_user_channels'"))
        db.session.commit()


def test_toggle_and_remove_user_channel(db, clean, monkeypatch):
    from app.modules.scrape.service import (
        add_user_channel,
        remove_user_channel,
        toggle_user_channel,
    )

    monkeypatch.setattr(_RESOLVE_CHANNEL_TARGET, lambda raw: (_resolved(), None))
    channel, _err = add_user_channel(clean, "@somechannel")

    assert toggle_user_channel(clean, channel.id) is False
    assert toggle_user_channel(clean, channel.id) is True
    assert toggle_user_channel(clean, 999_999) is None  # not found

    assert remove_user_channel(clean, channel.id) is True
    assert remove_user_channel(clean, channel.id) is False  # already gone


# ----------------------------------------------------------------------------
# ingest_user_channels — the channel counterpart of ingest_user_feeds
# (tests/modules/test_topics.py). `fetch_channel_videos` is monkeypatched at
# its source module, same reasoning as `_RESOLVE_CHANNEL_TARGET` above:
# `ingest_user_channels` imports it locally inside the function.
# ----------------------------------------------------------------------------

_FETCH_CHANNEL_VIDEOS_TARGET = (
    "app.modules.scrape.sources.youtube_channel_source.fetch_channel_videos"
)


def _video_payload(video_id: str, *, published_at=None) -> PaperPayload:
    return PaperPayload(
        source="youtube_channel",
        external_id=video_id,
        title=f"Video {video_id}",
        abstract="a video description",
        authors=["Some Channel"],
        url=f"https://www.youtube.com/watch?v={video_id}",
        pdf_url=None,
        published_at=published_at or datetime(2026, 7, 1, tzinfo=UTC),
        categories=["video"],
        kind="video",
        doi=None,
    )


def test_ingest_user_channels_creates_and_links_papers(db, clean, monkeypatch):
    from app.modules.scrape.models import UserChannel
    from app.modules.scrape.service import ingest_user_channels

    row = UserChannel(
        user_id=clean.id,
        channel_id="UC1111111111111111111111",
        title="Test Channel",
        url="https://www.youtube.com/channel/UC1111111111111111111111",
        active=True,
    )
    db.session.add(row)
    db.session.commit()

    payloads = [
        _video_payload("vid-1", published_at=datetime(2026, 7, 1, tzinfo=UTC)),
        _video_payload("vid-2", published_at=datetime(2026, 7, 5, tzinfo=UTC)),
    ]
    monkeypatch.setattr(
        _FETCH_CHANNEL_VIDEOS_TARGET,
        lambda channel_id, *, etag=None, last_modified=None, max_entries=15: (
            payloads,
            "ok",
            '"etag-1"',
            "Wed, 01 Jul 2026 00:00:00 GMT",
            "Test Channel",
        ),
    )

    summary, new_ids = ingest_user_channels(clean)
    assert summary == {"UC1111111111111111111111": 2}
    assert len(new_ids) == 2

    rows = Paper.query.filter_by(source="youtube_channel", kind="video").all()
    assert len(rows) == 2
    assert {r.external_id for r in rows} == {"vid-1", "vid-2"}

    linked_ids = {up.paper_id for up in UserPaper.query.filter_by(user_id=clean.id).all()}
    assert linked_ids == {r.id for r in rows}

    db.session.refresh(row)
    assert row.etag == '"etag-1"'
    assert row.last_modified == "Wed, 01 Jul 2026 00:00:00 GMT"
    assert row.last_video_at == datetime(2026, 7, 5, tzinfo=UTC)


def test_ingest_user_channels_not_modified_stores_nothing(db, clean, monkeypatch):
    from app.modules.scrape.models import UserChannel
    from app.modules.scrape.service import ingest_user_channels

    row = UserChannel(
        user_id=clean.id,
        channel_id="UC2222222222222222222222",
        url="https://www.youtube.com/channel/UC2222222222222222222222",
        active=True,
        etag='"old-etag"',
    )
    db.session.add(row)
    db.session.commit()

    monkeypatch.setattr(
        _FETCH_CHANNEL_VIDEOS_TARGET,
        lambda channel_id, *, etag=None, last_modified=None, max_entries=15: (
            [],
            "not_modified",
            etag,
            last_modified,
            None,
        ),
    )

    summary, new_ids = ingest_user_channels(clean)
    assert summary == {"UC2222222222222222222222": 0}
    assert new_ids == []
    db.session.refresh(row)
    assert row.etag == '"old-etag"'  # untouched — not_modified is not an update
    assert Paper.query.filter_by(source="youtube_channel").count() == 0


def test_ingest_user_channels_http_error_records_sentinel_and_continues(db, clean, monkeypatch):
    from app.modules.scrape.models import UserChannel
    from app.modules.scrape.service import ingest_user_channels

    bad = UserChannel(
        user_id=clean.id,
        channel_id="UC3333333333333333333333",
        url="https://www.youtube.com/channel/UC3333333333333333333333",
        active=True,
    )
    good = UserChannel(
        user_id=clean.id,
        channel_id="UC4444444444444444444444",
        url="https://www.youtube.com/channel/UC4444444444444444444444",
        active=True,
    )
    db.session.add_all([bad, good])
    db.session.commit()

    def _fetch(channel_id, *, etag=None, last_modified=None, max_entries=15):
        if channel_id == "UC3333333333333333333333":
            return [], "http_error", None, None, None
        return [_video_payload("vid-good")], "ok", None, None, "Good Channel"

    monkeypatch.setattr(_FETCH_CHANNEL_VIDEOS_TARGET, _fetch)

    summary, new_ids = ingest_user_channels(clean)
    assert summary["UC3333333333333333333333"] == -1
    assert summary["UC4444444444444444444444"] == 1
    assert len(new_ids) == 1


def test_ingest_user_channels_skips_inactive(db, clean, monkeypatch):
    from app.modules.scrape.models import UserChannel
    from app.modules.scrape.service import ingest_user_channels

    row = UserChannel(
        user_id=clean.id,
        channel_id="UC5555555555555555555555",
        url="https://www.youtube.com/channel/UC5555555555555555555555",
        active=False,
    )
    db.session.add(row)
    db.session.commit()

    def _boom(channel_id, *, etag=None, last_modified=None, max_entries=15):
        raise AssertionError("an inactive channel must not be fetched")

    monkeypatch.setattr(_FETCH_CHANNEL_VIDEOS_TARGET, _boom)

    summary, new_ids = ingest_user_channels(clean)
    assert summary == {}
    assert new_ids == []


# ----------------------------------------------------------------------------
# list_user_papers(kinds=...) — the home-page "burial" fix. Academic sources
# (kind=None/"paper") publish daily in volume and always win the top slots
# when ordered by published_at across every kind; `kinds` lets a caller
# scope to just the low-volume kinds (video/news) instead.
# ----------------------------------------------------------------------------


def test_list_user_papers_kinds_filters_to_matching_rows(db, clean):
    paper = upsert_paper(_payload("2401.90001"))
    video = upsert_paper(_video_payload("vid-kinds-1"))
    link_user_paper(clean, paper, matched_keyword="x")
    link_user_paper(clean, video, matched_keyword="x")

    only_video = list_user_papers(clean, kinds=("video",))
    assert len(only_video) == 1
    assert only_video[0].paper.kind == "video"


def test_list_user_papers_without_kinds_returns_everything(db, clean):
    """Default (no kinds) must be unaffected — guards against the new filter
    changing behaviour for every existing caller that doesn't pass it."""
    paper = upsert_paper(_payload("2401.90002"))
    video = upsert_paper(_video_payload("vid-kinds-2"))
    link_user_paper(clean, paper, matched_keyword="x")
    link_user_paper(clean, video, matched_keyword="x")

    rows = list_user_papers(clean)
    assert len(rows) == 2
