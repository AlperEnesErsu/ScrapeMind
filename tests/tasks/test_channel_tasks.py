"""Faz 3 — channel_tasks: per-user channel ingestion fan-out + per-video
transcript summarization.

No real network, no real subprocess, no real broker: `CELERY_TASK_ALWAYS_EAGER`
(TestingConfig) makes `.delay()` run inline, `ingest_user_channels`/
`fetch_transcript`/`generate_video_summary`/`ai_service.is_ai_enabled` are all
monkeypatched at the module the task imports them from (task functions use
function-local imports, same convention as `feed_tasks.py`/`scrape_tasks.py`),
and `apply_async`/`.delay` are monkeypatched wherever a test only cares that a
job was *queued*, not that it ran.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape import ai_service
from app.modules.scrape.models import Paper, VideoSummary
from app.tasks import channel_tasks


@pytest.fixture
def clean_user(db):
    for tbl in (
        "video_summaries",
        "user_papers",
        "papers",
        "user_channels",
        "scan_runs",
        "user_settings",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="channeltaskuser").delete()
    db.session.commit()
    u = User(
        username="channeltaskuser",
        email="channeltaskuser@example.test",
        full_name="Channel Task User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    for tbl in (
        "video_summaries",
        "user_papers",
        "papers",
        "user_channels",
        "scan_runs",
        "user_settings",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(id=u.id).delete()
    db.session.commit()


def _video_paper(db, *, external_id: str = "vid-1") -> Paper:
    p = Paper(
        source="youtube_channel",
        external_id=external_id,
        title="A Video",
        abstract="a description",
        authors=["Some Channel"],
        url=f"https://www.youtube.com/watch?v={external_id}",
        published_at=datetime(2026, 7, 1, tzinfo=UTC),
        categories=["video"],
        kind="video",
    )
    db.session.add(p)
    db.session.commit()
    return p


# ----------------------------------------------------------------------------
# channels.summarize_video — degrade-silently outcomes
# ----------------------------------------------------------------------------


def test_summarize_video_no_transcript_writes_no_row(app, db, clean_user, monkeypatch):
    with app.app_context():
        paper = _video_paper(db)
        monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
        monkeypatch.setattr(
            "app.modules.scrape.sources.youtube_channel_source.fetch_transcript",
            lambda video_id, **kw: None,
        )

        def _boom(*a, **kw):
            raise AssertionError("no LLM call should happen without a transcript")

        monkeypatch.setattr(ai_service, "generate_video_summary", _boom)

        result = channel_tasks.summarize_video.delay(paper.id).get()
        assert result == {"reason": "no_transcript"}
        assert VideoSummary.query.filter_by(paper_id=paper.id).count() == 0


def test_summarize_video_skips_when_already_summarized(app, db, clean_user, monkeypatch):
    with app.app_context():
        paper = _video_paper(db)
        db.session.add(VideoSummary(paper_id=paper.id, tldr="already there"))
        db.session.commit()

        def _boom(*a, **kw):
            raise AssertionError("must not fetch a transcript for an already-summarized video")

        monkeypatch.setattr(
            "app.modules.scrape.sources.youtube_channel_source.fetch_transcript", _boom
        )

        result = channel_tasks.summarize_video.delay(paper.id).get()
        assert result == {"reason": "already_summarized"}
        assert VideoSummary.query.filter_by(paper_id=paper.id).count() == 1


def test_summarize_video_skips_when_ai_disabled(app, db, clean_user, monkeypatch):
    with app.app_context():
        paper = _video_paper(db)
        monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)

        def _boom(*a, **kw):
            raise AssertionError("must not fetch a transcript when AI is disabled")

        monkeypatch.setattr(
            "app.modules.scrape.sources.youtube_channel_source.fetch_transcript", _boom
        )

        result = channel_tasks.summarize_video.delay(paper.id).get()
        assert result == {"reason": "ai_disabled"}
        assert VideoSummary.query.filter_by(paper_id=paper.id).count() == 0


def test_summarize_video_missing_paper_is_a_noop(app, db, clean_user):
    with app.app_context():
        result = channel_tasks.summarize_video.delay(10_000_000).get()
        assert result == {"reason": "paper_missing"}


def test_summarize_video_skips_non_video_papers(app, db, clean_user):
    with app.app_context():
        p = Paper(source="arxiv", external_id="2401.99999", title="A Paper", kind=None)
        db.session.add(p)
        db.session.commit()
        result = channel_tasks.summarize_video.delay(p.id).get()
        assert result == {"reason": "not_a_video"}


def test_summarize_video_happy_path_persists_summary(app, db, clean_user, monkeypatch):
    with app.app_context():
        paper = _video_paper(db)
        monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
        monkeypatch.setattr(
            "app.modules.scrape.sources.youtube_channel_source.fetch_transcript",
            lambda video_id, **kw: "some transcript text",
        )
        monkeypatch.setattr(
            ai_service,
            "_call_llm",
            lambda **kw: (
                {"tldr": "özet", "highlights": ["a", "b", "c"], "topics": ["ai"]},
                "{}",
            ),
        )

        result = channel_tasks.summarize_video.delay(paper.id).get()
        assert result["paper_id"] == paper.id
        row = VideoSummary.query.filter_by(paper_id=paper.id).first()
        assert row is not None
        assert row.tldr == "özet"


# ----------------------------------------------------------------------------
# channels.ingest_for_user — bounded summarize fan-out
# ----------------------------------------------------------------------------


def test_ingest_for_user_dispatches_at_most_max_per_run(app, db, clean_user, monkeypatch):
    monkeypatch.setitem(app.config, "CHANNEL_SUMMARY_MAX_PER_RUN", 2)
    new_ids = [101, 102, 103, 104]
    monkeypatch.setattr(
        "app.modules.scrape.service.ingest_user_channels",
        lambda user: ({"UC1": 4}, new_ids),
    )
    queued = []
    monkeypatch.setattr(
        channel_tasks.summarize_video,
        "delay",
        lambda paper_id, **kw: queued.append((paper_id, kw.get("user_id"))),
    )

    with app.app_context():
        # `.run(...)` calls the task body directly rather than going through
        # `.delay()`/`Task.__call__` — this test reads a monkeypatched
        # `app.config` value, and Celery's own app-context wrapping binds to
        # whichever Flask app last called `init_celery()` (a `PromiseProxy`
        # evaluates once and caches it), which in a full test-suite run can
        # be a throwaway app from an unrelated test module rather than this
        # fixture's `app`. Calling `.run()` executes in the context this
        # test already pushed, so the monkeypatched config is the one read.
        result = channel_tasks.ingest_for_user.run(clean_user.id)
        assert result["sources"] == {"UC1": 4}
        assert result["new"] == 4
        # Capped at CHANNEL_SUMMARY_MAX_PER_RUN, and each job carries the
        # subscriber so their own LLM key can pay for the call.
        assert queued == [(101, clean_user.id), (102, clean_user.id)]


def test_ingest_for_user_missing_user_is_a_noop(app, db):
    with app.app_context():
        result = channel_tasks.ingest_for_user.delay(10_000_000).get()
        assert result == {"reason": "user_missing"}


def test_channel_ingest_fanout_queues_active_users(app, db, clean_user, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.tasks.channel_tasks.ingest_for_user.apply_async",
        lambda **kw: calls.append(kw),
    )
    with app.app_context():
        result = channel_tasks.ingest_for_all_users()
        assert result["queued"] >= 1
        assert any(c["args"] == (clean_user.id,) for c in calls)
