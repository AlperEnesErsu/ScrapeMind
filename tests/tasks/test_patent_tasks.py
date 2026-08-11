"""Nightly patent scanning (Faz 5.2).

Follows `tests/tasks/test_channel_tasks.py`: the service function is
monkeypatched on the module the task imports it *through*, and the task runs
eagerly.
"""

from __future__ import annotations

import pytest
from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.modules.scrape.models import ScanRun
from app.tasks import patent_tasks


@pytest.fixture
def a_user(db):
    user = User.query.filter_by(email="patentuser@example.test").first()
    if user:
        return user
    user = User(
        username="patentuser",
        email="patentuser@example.test",
        full_name="Patent User",
        password_hash=generate_password_hash("password123"),
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture(autouse=True)
def clean_runs(db, request):
    yield
    user = User.query.filter_by(email="patentuser@example.test").first()
    if user:
        ScanRun.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.commit()


def test_missing_user_is_a_noop(app, db):
    assert patent_tasks.ingest_for_user(999_999_999) == {"reason": "user_missing"}


def test_run_records_a_patents_scan_run(app, db, a_user, monkeypatch):
    """A separate ScanRun kind from the academic scan — an exhausted patent
    quota must not be able to mark the academic run partial."""
    monkeypatch.setattr(
        "app.modules.scrape.service.scrape_patents_for_user",
        lambda user, **kw: {"hits": 3, "linked": 2, "sources": {"epo_ops": 3}},
    )

    result = patent_tasks.ingest_for_user(a_user.id)
    assert result["hits"] == 3

    run = ScanRun.query.filter_by(user_id=a_user.id, kind="patents").one()
    assert run.status == "ok"
    assert run.new_items == 2


def test_failing_source_marks_the_run_partial(app, db, a_user, monkeypatch):
    monkeypatch.setattr(
        "app.modules.scrape.service.scrape_patents_for_user",
        lambda user, **kw: {"hits": 0, "linked": 0, "sources": {"epo_ops": -1}},
    )

    patent_tasks.ingest_for_user(a_user.id)
    run = ScanRun.query.filter_by(user_id=a_user.id, kind="patents").one()
    assert run.status == "partial"


def test_no_sources_is_recorded_as_skipped(app, db, a_user, monkeypatch):
    monkeypatch.setattr(
        "app.modules.scrape.service.scrape_patents_for_user",
        lambda user, **kw: {"hits": 0, "linked": 0, "reason": "no_sources"},
    )

    patent_tasks.ingest_for_user(a_user.id)
    run = ScanRun.query.filter_by(user_id=a_user.id, kind="patents").one()
    assert run.status == "skipped"


def test_fanout_short_circuits_without_patent_sources(app, db, monkeypatch):
    """The common case — no EPO/PatentsView keys — must cost one registry
    lookup, not a queued task per user."""
    monkeypatch.setattr("app.modules.scrape.service.patent_sources_available", lambda: False)

    def boom(_task):
        raise AssertionError("should not have fanned out")

    monkeypatch.setattr(patent_tasks, "fan_out", boom)
    assert patent_tasks.ingest_for_all_users() == {"queued": 0, "reason": "no_patent_sources"}


def test_fanout_queues_users_when_sources_exist(app, db, monkeypatch):
    monkeypatch.setattr("app.modules.scrape.service.patent_sources_available", lambda: True)
    monkeypatch.setattr(patent_tasks, "fan_out", lambda task: 7)
    assert patent_tasks.ingest_for_all_users() == {"queued": 7}


def test_task_is_registered_and_routed():
    """A task module missing from app/tasks/__init__.py's import tuple is
    invisible to the worker and fails silently — and a missing TASK_ROUTES
    entry lands it on the wrong queue."""
    from app.tasks import TASK_ROUTES, celery_app

    assert "patents.ingest_for_user" in celery_app.tasks
    assert "patents.ingest_for_all_users" in celery_app.tasks
    assert TASK_ROUTES["patents.ingest_for_user"]["queue"] == "io"


def test_beat_runs_patents_between_channels_and_scrape():
    from app.tasks.schedule import BEAT_SCHEDULE

    entry = BEAT_SCHEDULE["patents-ingest-nightly"]
    assert entry["task"] == "patents.ingest_for_all_users"
    # 02:55 channels < 03:05 patents < 03:15 scrape. Local time
    # (Europe/Istanbul), not UTC — see app/tasks/schedule.py.
    assert entry["schedule"].hour == {3}
    assert entry["schedule"].minute == {5}
