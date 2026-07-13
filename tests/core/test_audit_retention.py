"""Audit log retention — the purge respects the window and the disable switch."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.audit.retention import purge_expired
from app.core.models.audit import AuditLog


@pytest.fixture
def aged_logs(db):
    db.session.execute(text("DELETE FROM audit_logs"))
    db.session.commit()
    now = datetime.now(UTC)
    db.session.add_all(
        [
            AuditLog(action="old.one", created_at=now - timedelta(days=400)),
            AuditLog(action="old.two", created_at=now - timedelta(days=181)),
            AuditLog(action="recent.one", created_at=now - timedelta(days=10)),
            AuditLog(action="recent.two", created_at=now - timedelta(hours=1)),
        ]
    )
    db.session.commit()
    yield
    db.session.execute(text("DELETE FROM audit_logs"))
    db.session.commit()


def test_purge_deletes_only_expired_rows(db, aged_logs):
    deleted = purge_expired(retention_days=180)
    assert deleted == 2
    remaining = [row.action for row in AuditLog.query.all()]
    assert sorted(remaining) == ["recent.one", "recent.two"]


def test_purge_disabled_when_zero(db, aged_logs):
    assert purge_expired(retention_days=0) == 0
    assert AuditLog.query.count() == 4


def test_purge_noop_when_nothing_expired(db, aged_logs):
    assert purge_expired(retention_days=500) == 0
    assert AuditLog.query.count() == 4


def test_purge_reads_config_default(app, db, aged_logs):
    # No explicit override → the configured AUDIT_RETENTION_DAYS applies.
    app.config["AUDIT_RETENTION_DAYS"] = 30
    assert purge_expired() == 2
    assert AuditLog.query.count() == 2


def test_celery_task_wraps_service(app, db, aged_logs):
    # CELERY_TASK_ALWAYS_EAGER=True in TestingConfig → runs inline.
    from app.tasks.core_tasks import purge_audit_logs

    app.config["AUDIT_RETENTION_DAYS"] = 180
    result = purge_audit_logs.delay().get()
    assert result == {"deleted": 2}
