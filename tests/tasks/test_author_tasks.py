"""Nightly author ingestion (Faz 5.4).

Follows `tests/tasks/test_patent_tasks.py`: the service function is
monkeypatched on the module the task imports it *through*, and the task runs
eagerly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.modules.scrape.models import ScanRun
from app.tasks import author_tasks


@pytest.fixture
def a_user(db):
    user = User.query.filter_by(email="authortask@example.test").first()
    if user is None:
        user = User(
            username="authortaskuser",
            email="authortask@example.test",
            full_name="Author Task User",
            password_hash=generate_password_hash("password123"),
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
    uid = user.id

    yield user

    db.session.rollback()
    db.session.execute(text("DELETE FROM scan_runs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


def test_missing_user_is_a_noop(app, db):
    assert author_tasks.ingest_for_user(999_999_999) == {"reason": "user_missing"}


def test_run_records_an_authors_scan_run(app, db, a_user, monkeypatch):
    monkeypatch.setattr(
        "app.modules.scrape.service.ingest_user_authors",
        lambda user, **kw: {"hits": 4, "linked": 3, "sources": {"Jane Smith": 4}},
    )

    result = author_tasks.ingest_for_user(a_user.id)
    assert result["hits"] == 4

    run = ScanRun.query.filter_by(user_id=a_user.id, kind="authors").one()
    assert run.status == "ok"
    assert run.new_items == 3


def test_failing_author_marks_the_run_partial(app, db, a_user, monkeypatch):
    monkeypatch.setattr(
        "app.modules.scrape.service.ingest_user_authors",
        lambda user, **kw: {"hits": 0, "linked": 0, "sources": {"Jane Smith": -1}},
    )

    author_tasks.ingest_for_user(a_user.id)
    assert ScanRun.query.filter_by(user_id=a_user.id, kind="authors").one().status == "partial"


def test_following_nobody_is_recorded_as_skipped(app, db, a_user, monkeypatch):
    monkeypatch.setattr(
        "app.modules.scrape.service.ingest_user_authors",
        lambda user, **kw: {"hits": 0, "linked": 0, "reason": "no_authors"},
    )

    author_tasks.ingest_for_user(a_user.id)
    assert ScanRun.query.filter_by(user_id=a_user.id, kind="authors").one().status == "skipped"


def test_fanout_always_queues(app, db, monkeypatch):
    """Unlike patents there is no deployment-level gate — following authors
    needs no keys and no admin opt-in."""
    monkeypatch.setattr(author_tasks, "fan_out", lambda task: 5)
    assert author_tasks.ingest_for_all_users() == {"queued": 5}


def test_task_is_registered_and_routed_to_scrape():
    """A task module missing from app/tasks/__init__.py's import tuple is
    invisible to the worker and fails silently."""
    from app.tasks import TASK_ROUTES, celery_app

    assert "authors.ingest_for_user" in celery_app.tasks
    assert "authors.ingest_for_all_users" in celery_app.tasks
    # Same queue as the academic scan: both spend the same OpenAlex budget.
    assert TASK_ROUTES["authors.ingest_for_user"]["queue"] == "scrape"


def test_beat_runs_authors_after_the_academic_scan():
    from app.tasks.schedule import BEAT_SCHEDULE

    entry = BEAT_SCHEDULE["authors-ingest-nightly"]
    assert entry["task"] == "authors.ingest_for_all_users"
    # 03:15 scrape < 03:25 authors, so the two never contend for the same
    # OpenAlex budget. Local time (Europe/Istanbul), not UTC.
    assert entry["schedule"].hour == {3}
    assert entry["schedule"].minute == {25}
