import structlog
from flask import g, request
from flask_login import current_user

from app.core.models.audit import AuditLog
from app.extensions import db

logger = structlog.get_logger()


def log_action(
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    changes: dict | None = None,
    user_id: int | None = None,
) -> None:
    """Write a structured audit entry. Safe to call outside request context.

    `user_id` overrides the actor — pass it for token-authenticated API calls
    where flask-login's current_user is anonymous. Falls back to current_user.
    """
    if user_id is None:
        try:
            user_id = current_user.id if current_user and current_user.is_authenticated else None
        except Exception:  # noqa: BLE001
            user_id = None
    entry = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        changes=changes,
        ip_address=request.remote_addr if request else None,
        user_agent=request.user_agent.string if request else None,
        locale=getattr(g, "locale", None),
    )
    db.session.add(entry)
    db.session.commit()
    logger.info("audit", action=action, user_id=user_id, entity_type=entity_type)
