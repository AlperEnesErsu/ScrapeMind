"""Scan visibility — ScanRun history, resolved next-run times, and the UI
that now states rather than guesses when a user was last/next scanned.

Before this, "last update" was max(user_papers.created_at) — a proxy that
goes stale the moment a scan finds nothing new — and "every night at 03:15"
was a string literal that nothing kept in sync with the beat schedule. These
tests exist mostly to keep both fictions from coming back.

No real network: source adapters are stubbed the same way test_scrape.py does
it, and the LLM is never reached.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.service import add_user_keyword
from app.modules.scrape.models import ScanRun
from app.modules.scrape.service import (
    apply_scan_result,
    last_scan_run,
    link_user_paper,
    purge_scan_runs,
    record_scan_run,
    scan_status_context,
    upsert_paper,
)
from app.modules.scrape.sources.payload import PaperPayload
from app.tasks.fanout import user_fanout_offset
from app.tasks.schedule_info import next_run_at, next_scan_at_for_user, schedule_overview


@pytest.fixture
def clean_user(db):
    for tbl in (
        "notifications",
        "user_digests",
        "paper_notes",
        "user_papers",
        "papers",
        "user_sources",
        "user_feeds",
        "scan_runs",
        "user_settings",
        "user_keywords",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="scanuser").delete()
    db.session.commit()
    u = User(
        username="scanuser",
        email="scanuser@example.test",
        full_name="Scan User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    for tbl in (
        "notifications",
        "user_digests",
        "paper_notes",
        "user_papers",
        "papers",
        "user_sources",
        "user_feeds",
        "scan_runs",
        "user_settings",
        "user_keywords",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(id=u.id).delete()
    db.session.commit()


def _payload(ext_id: str = "2401.00001") -> PaperPayload:
    return PaperPayload(
        source="fake",
        external_id=ext_id,
        title="A Paper About Quantum Things",
        abstract="Abstract.",
        authors=["A. Author"],
        url=f"https://example.test/{ext_id}",
        pdf_url=None,
        published_at=datetime(2026, 7, 20, tzinfo=UTC),
        categories=["cs.AI"],
    )


def _video_payload(video_id: str) -> PaperPayload:
    return PaperPayload(
        source="youtube_channel",
        external_id=video_id,
        title=f"Video {video_id}",
        abstract="a video description",
        authors=["Some Channel"],
        url=f"https://www.youtube.com/watch?v={video_id}",
        pdf_url=None,
        published_at=datetime(2026, 7, 25, tzinfo=UTC),
        categories=["video"],
        kind="video",
    )


def _stub_source(monkeypatch, payloads=None, *, boom=False):
    """Replace the user's enabled sources with one fake adapter."""

    def _search(keywords, *, max_results=25):
        if boom:
            raise RuntimeError("source exploded")
        return payloads or []

    monkeypatch.setattr(
        "app.modules.scrape.service.user_enabled_sources",
        lambda user: {"fake": SimpleNamespace(search_for_keywords=_search)},
    )


# ----------------------------------------------------------------------------
# ScanRun rows — the scrape task records what actually happened
# ----------------------------------------------------------------------------


def test_scrape_task_writes_a_finished_scan_run(app, db, clean_user, monkeypatch):
    from app.tasks.scrape_tasks import run_for_user

    _stub_source(monkeypatch, [_payload()])
    with app.app_context():
        add_user_keyword(clean_user, "quantum")
        run_for_user.delay(clean_user.id).get()

        run = last_scan_run(clean_user, "scrape")
        assert run is not None
        assert run.status == "ok"
        assert run.trigger == "auto"
        assert run.new_items == 1
        assert run.finished_at is not None
        assert run.duration_ms is not None and run.duration_ms >= 0
        assert run.details["sources"] == {"fake": 1}


def test_scan_run_marks_partial_when_a_source_fails(app, db, clean_user, monkeypatch):
    """The -1 per-source sentinel must surface as "partial", not "ok" — a scan
    that lost a source is not the same as a clean one."""
    from app.tasks.scrape_tasks import run_for_user

    _stub_source(monkeypatch, boom=True)
    with app.app_context():
        add_user_keyword(clean_user, "quantum")
        run_for_user.delay(clean_user.id).get()

        run = last_scan_run(clean_user, "scrape")
        assert run.status == "partial"
        assert run.details["sources"] == {"fake": -1}


def test_scan_run_marks_skipped_without_keywords(app, db, clean_user):
    """A short-circuited run is recorded but must not read as a real scan."""
    from app.tasks.scrape_tasks import run_for_user

    with app.app_context():
        run_for_user.delay(clean_user.id).get()
        run = last_scan_run(clean_user, "scrape")
        assert run.status == "skipped"
        assert run.details["reason"] == "no_keywords"


def test_scan_run_records_error_and_reraises(app, db, clean_user, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(
            "app.modules.scrape.service.user_enabled_sources",
            lambda user: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        add_user_keyword(clean_user, "quantum")
        with pytest.raises(RuntimeError):
            with record_scan_run(clean_user.id, "scrape") as run:
                from app.modules.scrape.service import scrape_for_user

                apply_scan_result(run, scrape_for_user(clean_user))

        row = last_scan_run(clean_user, "scrape")
        assert row.status == "error"
        assert row.details["error"] == "RuntimeError"
        assert row.finished_at is not None  # still closed out, not left "running"


def test_manual_run_is_recorded_as_manual(app, db, clean_user, monkeypatch):
    from app.tasks.scrape_tasks import run_for_user

    _stub_source(monkeypatch, [_payload("2401.00002")])
    with app.app_context():
        add_user_keyword(clean_user, "quantum")
        run_for_user.delay(clean_user.id, trigger="manual", lock_held=True).get()
        assert last_scan_run(clean_user, "scrape").trigger == "manual"


def test_purge_scan_runs_respects_retention(app, db, clean_user):
    with app.app_context():
        old = ScanRun(
            user_id=clean_user.id,
            kind="scrape",
            trigger="auto",
            status="ok",
            started_at=datetime.now(UTC) - timedelta(days=40),
            finished_at=datetime.now(UTC) - timedelta(days=40),
        )
        recent = ScanRun(
            user_id=clean_user.id,
            kind="scrape",
            trigger="auto",
            status="ok",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        db.session.add_all([old, recent])
        db.session.commit()

        assert purge_scan_runs(0) == 0  # 0 disables purging
        assert ScanRun.query.filter_by(user_id=clean_user.id).count() == 2

        assert purge_scan_runs(30) == 1
        remaining = ScanRun.query.filter_by(user_id=clean_user.id).all()
        assert len(remaining) == 1 and remaining[0].id == recent.id


# ----------------------------------------------------------------------------
# Next-run resolution — derived from the beat schedule, in the app timezone
# ----------------------------------------------------------------------------


def test_next_run_at_reads_the_beat_schedule(app):
    with app.app_context():
        when = next_run_at("scrape.run_for_all_users")
        assert when is not None
        assert when > datetime.now(UTC)


def test_next_run_at_resolves_in_the_app_timezone(app):
    """Crontabs match in conf.timezone (Europe/Istanbul), not UTC. If this ever
    regresses the dashboard shows an hour that Beat will not fire at."""
    with app.app_context():
        when = next_run_at("scrape.run_for_all_users")
        local = when.astimezone(ZoneInfo(app.config["BABEL_DEFAULT_TIMEZONE"]))
        assert (local.hour, local.minute) == (3, 15)


def test_next_run_at_is_none_for_an_unscheduled_task(app):
    with app.app_context():
        assert next_run_at("scrape.no_such_task") is None


def test_next_scan_for_user_adds_their_fanout_offset(app, db, clean_user):
    """The displayed time has to include the user's own spread offset, or it
    is off by up to SCAN_FANOUT_WINDOW_SECONDS."""
    with app.app_context():
        base = next_run_at("scrape.run_for_all_users")
        mine = next_scan_at_for_user(clean_user)
        assert mine - base == timedelta(seconds=user_fanout_offset(clean_user.id))


def test_schedule_overview_lists_entries_with_queue_and_next_run(app):
    with app.app_context():
        rows = schedule_overview()
        by_task = {r["task"]: r for r in rows}
        assert "scrape.run_for_all_users" in by_task
        assert by_task["scrape.run_for_all_users"]["queue"] == "scrape"
        assert by_task["core.heartbeat"]["queue"] == "celery"  # default queue
        assert all(r["next_run_at"] is not None for r in rows)


def test_celery_time_limits_and_routes_are_configured(app):
    """Without a time limit, one hung HTTP call pins a worker slot forever."""
    from app.tasks import celery_app

    with app.app_context():
        assert celery_app.conf.task_time_limit == app.config["CELERY_TASK_TIME_LIMIT"]
        assert celery_app.conf.task_soft_time_limit == app.config["CELERY_TASK_SOFT_TIME_LIMIT"]
        assert celery_app.conf.task_acks_late is True
        assert celery_app.conf.worker_prefetch_multiplier == 1
        assert celery_app.conf.task_routes["feeds.ingest_all"]["queue"] == "io"


# ----------------------------------------------------------------------------
# scan_status_context + the templates that render it
# ----------------------------------------------------------------------------


def test_scan_status_context_before_any_run(app, db, clean_user):
    with app.app_context():
        ctx = scan_status_context(clean_user)
        assert ctx["last_run"] is None
        assert ctx["next_run_at"] is not None
        assert ctx["is_running"] is False
        assert ctx["estimated_seconds"] >= 1
        assert ctx["feed_count"] == 0


def test_scan_status_estimate_uses_the_users_own_history(app, db, clean_user):
    """After a real run the estimate is that user's median, not the static
    formula — self-correcting instead of a maintained cost model."""
    with app.app_context():
        for _ in range(3):
            db.session.add(
                ScanRun(
                    user_id=clean_user.id,
                    kind="scrape",
                    trigger="auto",
                    status="ok",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    duration_ms=42_000,
                )
            )
        db.session.commit()
        assert scan_status_context(clean_user)["estimated_seconds"] == 42


def test_scan_status_reports_an_open_run_as_running(app, db, clean_user):
    with app.app_context():
        db.session.add(
            ScanRun(
                user_id=clean_user.id,
                kind="scrape",
                trigger="auto",
                status="running",
                started_at=datetime.now(UTC),
            )
        )
        db.session.commit()
        ctx = scan_status_context(clean_user)
        assert ctx["is_running"] is True
        assert ctx["active_run"] is not None
        assert ctx["running_seconds"] is not None and ctx["running_seconds"] >= 0


def test_a_held_lock_alone_does_not_claim_a_scan_is_running(app, db, clean_user, monkeypatch):
    """The lock is claimed by the HTTP route *before* a worker picks the task
    up. Keying the indicator on it announced "scanning" for the full 15-minute
    TTL even when no worker existed — which is exactly what made it untrusted.
    Only a real ScanRun row counts."""
    monkeypatch.setattr(
        "app.modules.scrape.service._lock_client",
        lambda: SimpleNamespace(exists=lambda key: 1),
    )
    with app.app_context():
        assert scan_status_context(clean_user)["is_running"] is False


def test_abandoned_run_is_reaped_instead_of_scanning_forever(app, db, clean_user):
    """A worker SIGKILLed past its hard time limit never stamps finished_at.
    Without reaping, that row shows "scanning…" indefinitely."""
    from app.modules.scrape.service import reap_stale_runs

    with app.app_context():
        limit = app.config["CELERY_TASK_TIME_LIMIT"]
        db.session.add(
            ScanRun(
                user_id=clean_user.id,
                kind="scrape",
                trigger="auto",
                status="running",
                started_at=datetime.now(UTC) - timedelta(seconds=limit + 600),
            )
        )
        db.session.commit()

        ctx = scan_status_context(clean_user)  # reaps on read
        assert ctx["is_running"] is False
        run = last_scan_run(clean_user, "scrape")
        assert run.status == "error"
        assert run.details["error"] == "abandoned"
        assert run.finished_at is not None

        assert reap_stale_runs(clean_user) == 0  # idempotent


def test_a_young_open_run_is_not_reaped(app, db, clean_user):
    from app.modules.scrape.service import reap_stale_runs

    with app.app_context():
        db.session.add(
            ScanRun(
                user_id=clean_user.id,
                kind="scrape",
                trigger="auto",
                status="running",
                started_at=datetime.now(UTC) - timedelta(seconds=30),
            )
        )
        db.session.commit()
        assert reap_stale_runs(clean_user) == 0
        assert scan_status_context(clean_user)["is_running"] is True


def test_scan_status_endpoint_polls_only_while_running(auth_client, db):
    """Live indicator: the running version re-fetches itself, the idle version
    carries no trigger so polling stops on its own."""
    from app.core.models.user import User

    client, uid = auth_client

    idle = client.get("/papers/scan-status").get_data(as_text=True)
    assert 'id="scan-status"' in idle
    assert "hx-trigger" not in idle
    assert 'data-testid="next-scan"' in idle

    db.session.add(
        ScanRun(
            user_id=uid,
            kind="scrape",
            trigger="manual",
            status="running",
            started_at=datetime.now(UTC),
        )
    )
    db.session.commit()

    running = client.get("/papers/scan-status").get_data(as_text=True)
    assert 'data-testid="scan-running"' in running
    assert 'hx-trigger="every 5s"' in running
    assert "/papers/scan-status" in running

    db.session.query(ScanRun).filter_by(user_id=uid).delete()
    db.session.commit()
    _ = User.query.get(uid)  # keep the session attached for teardown


def test_for_you_shows_a_real_next_scan(auth_client, monkeypatch):
    """The dashboard must render a resolved time, not the old literal."""
    client, _uid = auth_client
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'data-testid="next-scan"' in body
    assert 'data-testid="last-scan"' in body
    # Turkish for "Never scanned yet" — a fresh user has no ScanRun rows
    assert "Henüz taranmadı" in body
    # The removed literal must not come back
    assert "her gece 03:15" not in body


# ----------------------------------------------------------------------------
# Manual-scrape spinner — must survive a missing/disabled Celery result backend
# ----------------------------------------------------------------------------


def test_status_poll_survives_a_disabled_result_backend(auth_client, db, monkeypatch):
    """`AsyncResult(task_id)` without an explicit app resolves through
    `celery.current_app`, which in a web process can be a bare default Celery
    app whose backend is DisabledBackend — `.ready()` then raises
    AttributeError and 500s the poll. A backend problem must degrade to "still
    waiting", never to a crash."""

    def _boom(*a, **kw):
        raise AttributeError("'DisabledBackend' object has no attribute '_get_task_meta_for'")

    monkeypatch.setattr("app.modules.scrape.routes._celery_task_finished", lambda tid: None)
    client, _uid = auth_client
    r = client.get(f"/papers/status/fake-task-id?since={int(time.time())}")
    assert r.status_code == 200
    assert 'data-testid="scrape-phase-queued"' in r.get_data(as_text=True)

    # And the real helper swallows the backend error rather than propagating it
    from app.modules.scrape import routes

    monkeypatch.setattr("celery.result.AsyncResult", _boom)
    assert routes._celery_task_finished("fake-task-id") is None


def test_status_poll_reports_running_once_a_run_row_exists(auth_client, db):
    client, uid = auth_client
    db.session.add(
        ScanRun(
            user_id=uid,
            kind="scrape",
            trigger="manual",
            status="running",
            started_at=datetime.now(UTC),
        )
    )
    db.session.commit()

    r = client.get(f"/papers/status/fake-task-id?since={int(time.time())}")
    body = r.get_data(as_text=True)
    assert 'data-testid="scrape-phase-running"' in body

    db.session.query(ScanRun).filter_by(user_id=uid).delete()
    db.session.commit()


def test_status_poll_gives_up_when_no_worker_picks_it_up(auth_client, db, monkeypatch):
    """The user's actual situation: task queued, no Celery worker consuming it.
    An endless spinner is the least useful way to say that."""
    monkeypatch.setattr("app.modules.scrape.routes._celery_task_finished", lambda tid: None)
    client, _uid = auth_client

    stale_since = int(time.time()) - 600
    r = client.get(f"/papers/status/fake-task-id?since={stale_since}")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'data-testid="worker-stalled"' in body
    assert "hx-trigger" not in body  # stops polling


def test_status_poll_finishes_when_a_run_completed_after_the_request(auth_client, db, monkeypatch):
    monkeypatch.setattr("app.modules.scrape.routes._celery_task_finished", lambda tid: None)
    client, uid = auth_client
    since = int(time.time())
    db.session.add(
        ScanRun(
            user_id=uid,
            kind="scrape",
            trigger="manual",
            status="ok",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            duration_ms=1200,
            new_items=3,
        )
    )
    db.session.commit()

    r = client.get(f"/papers/status/fake-task-id?since={since}")
    assert r.headers.get("HX-Refresh") == "true"
    assert 'id="scraper-widget"' in r.get_data(as_text=True)

    db.session.query(ScanRun).filter_by(user_id=uid).delete()
    db.session.commit()


def test_dashboard_renders_exactly_one_scrape_control(auth_client, db):
    """Both copies answered to #scraper-widget, so only one could ever win an
    HTMX swap. The dashboard now carries a single control, in the header."""
    from app.core.models.user import User
    from app.modules.academic.service import add_user_keyword

    client, uid = auth_client
    add_user_keyword(User.query.get(uid), "quantum")

    body = client.get("/").get_data(as_text=True)
    assert body.count('id="scraper-widget"') == 1
    # In the page header, i.e. above the main two-column content row
    assert body.index('id="scraper-widget"') < body.index('class="row g-4"')


def test_scrape_control_keeps_its_shape_through_the_swap_chain(auth_client, db, monkeypatch):
    """widget -> spinner -> widget all render the same inline root, so starting
    a scrape doesn't reflow the page header."""
    monkeypatch.setattr("app.modules.scrape.service.acquire_scrape_lock", lambda user_id: True)
    client, _uid = auth_client

    started = client.post("/papers/run", headers={"HX-Request": "true"})
    body = started.get_data(as_text=True)
    assert 'id="scraper-widget"' in body
    assert "card-header" not in body

    polled = client.get(f"/papers/status/x?since={int(time.time())}")
    polled_body = polled.get_data(as_text=True)
    assert 'id="scraper-widget"' in polled_body
    assert "card-header" not in polled_body


def test_scraper_widget_shows_the_users_effective_sources(auth_client, monkeypatch):
    """A source this user muted must not be advertised as one that will run —
    the widget used to read the deployment-wide list."""
    from app.core.models.user import User
    from app.modules.scrape.service import set_user_source

    client, uid = auth_client
    r = client.get("/papers/")
    assert "arxiv" in r.get_data(as_text=True)

    set_user_source(User.query.get(uid), "arxiv", False)
    body = client.get("/papers/").get_data(as_text=True)
    assert 'class="badge source-badge source-arxiv' not in body


# ----------------------------------------------------------------------------
# Home page — video/news section (burial fix) + clickable stat pills
# ----------------------------------------------------------------------------


def test_home_page_shows_video_in_channels_feeds_section(auth_client, db):
    """A video paper linked to the user renders on the home page even though
    it never makes the `for_you` top-5 (academic papers outrank it there)."""
    from app.core.models.user import User

    client, uid = auth_client
    user = User.query.get(uid)
    add_user_keyword(user, "quantum")

    academic = upsert_paper(_payload("2401.11111"))
    video = upsert_paper(_video_payload("vid-home-1"))
    up_academic, _ = link_user_paper(user, academic, matched_keyword="quantum")
    up_video, _ = link_user_paper(user, video, matched_keyword="quantum")

    body = client.get("/").get_data(as_text=True)
    assert f'id="card-{up_video.id}"' in body
    assert f'id="card-{up_academic.id}"' in body


def test_home_page_video_section_absent_when_no_video_or_news(auth_client, db):
    """No video/news linked → the section (and its heading) must not render
    at all, not just render empty."""
    client, _uid = auth_client
    body = client.get("/").get_data(as_text=True)
    assert "bi-broadcast" not in body


def test_home_page_does_not_duplicate_a_video_already_in_top_five(auth_client, db):
    """A video that lands in the top-5 `for_you` slots must not also be
    rendered again in the channels/feeds section below it."""
    from app.core.models.user import User

    client, uid = auth_client
    user = User.query.get(uid)
    add_user_keyword(user, "quantum")

    video = upsert_paper(_video_payload("vid-home-2"))
    up_video, _ = link_user_paper(user, video, matched_keyword="quantum")

    body = client.get("/").get_data(as_text=True)
    assert body.count(f'id="card-{up_video.id}"') == 1


def test_stat_pills_are_links_to_expected_destinations(auth_client, db):
    """The four stat pills must be real links (keyboard-reachable), not
    inert divs, pointing at interests/favorites/read-later/notes."""
    from flask import url_for

    client, _uid = auth_client
    with client.application.test_request_context():
        favorites_url = url_for("library.index", view="favorites")
        read_later_url = url_for("library.index", view="read_later")
        notes_url = url_for("library.index", view="notes")

    body = client.get("/").get_data(as_text=True)
    assert 'href="#interests-manager" class="stat-pill stat-pill--interests' in body
    assert f'href="{favorites_url}" class="stat-pill stat-pill--favorites' in body
    assert f'href="{read_later_url}" class="stat-pill stat-pill--read-later' in body
    assert f'href="{notes_url}" class="stat-pill stat-pill--notes' in body
