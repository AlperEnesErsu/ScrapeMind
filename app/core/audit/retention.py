"""Audit log retention — purge rows past the configured age.

Audit rows are append-only and grow without bound; `AUDIT_RETENTION_DAYS`
caps how far back we keep them (default 180 days, 0 = keep forever). The
nightly `core.purge_audit_logs` Celery task calls :func:`purge_expired`.

Hard delete on purpose: an expired audit row has no soft-delete story —
retention IS the deletion policy, and regulators care that expired data is
actually gone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from flask import current_app

from app.core.models.audit import AuditLog
from app.extensions import db

logger = structlog.get_logger()


def purge_expired(retention_days: int | None = None) -> int:
    """Delete audit rows older than the retention window. Returns rows removed.

    `retention_days` overrides config when given (used by tests and one-off
    ops runs); <= 0 means retention is disabled and nothing is deleted.
    """
    if retention_days is None:
        retention_days = int(current_app.config.get("AUDIT_RETENTION_DAYS", 0))
    if retention_days <= 0:
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    # Single bulk DELETE — no ORM object loading; created_at is indexed.
    deleted = AuditLog.query.filter(AuditLog.created_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    if deleted:
        logger.info("audit_purged", deleted=deleted, retention_days=retention_days)
    return deleted
