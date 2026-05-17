from datetime import datetime

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import distinct

from app.core.auth.decorators import permission_required
from app.core.models.audit import AuditLog
from app.core.models.user import User
from app.extensions import db

audit_bp = Blueprint("audit", __name__)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


@audit_bp.route("/")
@login_required
@permission_required("audit.view")
def log_list():
    page = request.args.get("page", 1, type=int)
    user_id = request.args.get("user_id", type=int) or None
    action = (request.args.get("action") or "").strip() or None
    entity_type = (request.args.get("entity_type") or "").strip() or None
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    q = AuditLog.query
    if user_id:
        q = q.filter(AuditLog.user_id == user_id)
    if action:
        q = q.filter(AuditLog.action.ilike(f"{action}%"))
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    if date_from:
        q = q.filter(AuditLog.created_at >= date_from)
    if date_to:
        # inclusive end-of-day
        end = date_to.replace(hour=23, minute=59, second=59)
        q = q.filter(AuditLog.created_at <= end)

    logs = q.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50)

    # Filter dropdown choices
    actions = [
        a[0] for a in db.session.query(distinct(AuditLog.action)).order_by(AuditLog.action).all()
    ]
    entity_types = [
        e[0]
        for e in db.session.query(distinct(AuditLog.entity_type))
        .filter(AuditLog.entity_type.isnot(None))
        .order_by(AuditLog.entity_type)
        .all()
    ]
    users = User.query.filter(User.deleted_at.is_(None)).order_by(User.username).all()

    return render_template(
        "audit/list.html",
        logs=logs,
        actions=actions,
        entity_types=entity_types,
        users=users,
        filters={
            "user_id": user_id,
            "action": action,
            "entity_type": entity_type,
            "date_from": request.args.get("date_from", ""),
            "date_to": request.args.get("date_to", ""),
        },
    )
