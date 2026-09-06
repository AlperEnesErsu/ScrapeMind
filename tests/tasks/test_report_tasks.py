"""On-demand report generation — `reports.generate` (Faz 6).

Follows `tests/tasks/test_patent_tasks.py`/`test_channel_tasks.py`: the
service functions the task imports through function-local imports
(`app.modules.scrape.report_service`/`app.modules.scrape.service`) are
monkeypatched on those modules, and Celery runs eagerly
(`CELERY_TASK_ALWAYS_EAGER` + `CELERY_TASK_EAGER_PROPAGATES` in
`TestingConfig`). Locking is exercised by monkeypatching
`acquire_user_lock`/`release_user_lock` directly rather than through a real
Redis contention window — `TestingConfig`'s `REDIS_URL` points at a closed
port on purpose (see CLAUDE.md), so the real lock is fail-open and would
never actually block a second call.
"""

from __future__ import annotations

import pytest
from celery.exceptions import Retry
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from app.core.models.notification import Notification
from app.core.models.user import User
from app.modules.scrape.models import Report
from app.tasks import report_tasks


@pytest.fixture
def a_user(db):
    user = User.query.filter_by(email="reporttaskuser@example.test").first()
    if user is None:
        user = User(
            username="reporttaskuser",
            email="reporttaskuser@example.test",
            full_name="Report Task User",
            password_hash=generate_password_hash("password123"),
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
    uid = user.id

    yield user

    db.session.rollback()
    db.session.execute(text("DELETE FROM notifications WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM reports WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


@pytest.fixture
def a_report(db, a_user):
    report = Report(
        user_id=a_user.id,
        kind="topic",
        title="Test Report",
        params={"keywords": ["ai"], "years": 1},
        status="pending",
    )
    db.session.add(report)
    db.session.commit()
    return report


def _run_report_ok(report):  # noqa: ARG001 — stand-in for report_service.run_report
    return {"status": "ok", "item_count": 5}


# ----------------------------------------------------------------------------
# guard — missing row / owner no longer around
# ----------------------------------------------------------------------------


def test_missing_report_is_a_noop(app, db):
    with app.app_context():
        result = report_tasks.generate.delay(999_999_999).get()
        assert result == {"reason": "report_missing"}


def test_deleted_owner_is_treated_as_report_missing(app, db, a_user, a_report, monkeypatch):
    """The task's only argument is `report_id` — it re-derives the owner from
    the row itself, so a user removed after the row was created must not
    have a report generated on their behalf."""

    def _boom(*a, **kw):
        raise AssertionError("must not call run_report for a deleted owner")

    monkeypatch.setattr("app.modules.scrape.report_service.run_report", _boom)

    a_user.soft_delete()
    db.session.commit()

    with app.app_context():
        result = report_tasks.generate.delay(a_report.id).get()
        assert result == {"reason": "report_missing"}


# ----------------------------------------------------------------------------
# locking
# ----------------------------------------------------------------------------


def test_locked_returns_reason_without_running(app, db, a_report, monkeypatch):
    monkeypatch.setattr("app.modules.scrape.service.acquire_user_lock", lambda uid, kind: False)

    def _boom(*a, **kw):
        raise AssertionError("must not call run_report while the report lock is held")

    monkeypatch.setattr("app.modules.scrape.report_service.run_report", _boom)

    with app.app_context():
        result = report_tasks.generate.delay(a_report.id).get()
        assert result == {"reason": "locked"}


def test_lock_is_released_on_success(app, db, a_user, a_report, monkeypatch):
    released = []
    monkeypatch.setattr("app.modules.scrape.service.acquire_user_lock", lambda uid, kind: True)
    monkeypatch.setattr(
        "app.modules.scrape.service.release_user_lock",
        lambda uid, kind: released.append((uid, kind)),
    )
    monkeypatch.setattr("app.modules.scrape.report_service.run_report", _run_report_ok)

    with app.app_context():
        result = report_tasks.generate.delay(a_report.id).get()
        assert result == {"status": "ok", "item_count": 5}
    assert released == [(a_user.id, "report")]


def test_lock_is_released_on_unexpected_exception(app, db, a_user, a_report, monkeypatch):
    """Same failure mode `scrape_tasks.run_for_user` documents: a lock still
    held while the retry backoff waits blocks the user from starting any
    other report for the whole window. Released in `finally` regardless."""
    released = []
    monkeypatch.setattr("app.modules.scrape.service.acquire_user_lock", lambda uid, kind: True)
    monkeypatch.setattr(
        "app.modules.scrape.service.release_user_lock",
        lambda uid, kind: released.append((uid, kind)),
    )

    def _boom(report):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.modules.scrape.report_service.run_report", _boom)

    with app.app_context():
        with pytest.raises(Retry):
            report_tasks.generate.delay(a_report.id).get()

    assert released == [(a_user.id, "report")]


# ----------------------------------------------------------------------------
# idempotency — acks_late redelivery of an already-finished report
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["ok", "partial", "error"])
def test_already_done_report_is_not_rerun(app, db, a_report, monkeypatch, status):
    a_report.status = status
    db.session.commit()

    def _boom(*a, **kw):
        raise AssertionError(f"must not re-run a report already in status={status!r}")

    monkeypatch.setattr("app.modules.scrape.report_service.run_report", _boom)
    monkeypatch.setattr("app.modules.scrape.service.acquire_user_lock", lambda uid, kind: True)
    monkeypatch.setattr("app.modules.scrape.service.release_user_lock", lambda uid, kind: None)

    with app.app_context():
        result = report_tasks.generate.delay(a_report.id).get()
        assert result == {"reason": "already_done"}


# ----------------------------------------------------------------------------
# notification
# ----------------------------------------------------------------------------


def test_success_sends_notification_built_from_app_base_url(app, db, a_user, a_report, monkeypatch):
    monkeypatch.setattr("app.modules.scrape.service.acquire_user_lock", lambda uid, kind: True)
    monkeypatch.setattr("app.modules.scrape.service.release_user_lock", lambda uid, kind: None)
    monkeypatch.setattr("app.modules.scrape.report_service.run_report", _run_report_ok)
    monkeypatch.setitem(app.config, "APP_BASE_URL", "https://reports.example.test")

    with app.app_context():
        report_tasks.generate.delay(a_report.id).get()

    note = Notification.query.filter_by(user_id=a_user.id).order_by(Notification.id.desc()).first()
    assert note is not None
    assert "https://reports.example.test" in note.message
    assert f"/papers/reports/{a_report.id}" in note.message
    assert "localhost" not in note.message


# ----------------------------------------------------------------------------
# routing / registration
# ----------------------------------------------------------------------------


def test_task_is_registered_and_routed_to_llm():
    """Missing from `app/tasks/__init__.py`'s import tuple → invisible to the
    worker with no error; missing from `TASK_ROUTES` → wrong queue."""
    from app.tasks import TASK_ROUTES, celery_app

    assert "reports.generate" in celery_app.tasks
    assert TASK_ROUTES["reports.generate"]["queue"] == "llm"
