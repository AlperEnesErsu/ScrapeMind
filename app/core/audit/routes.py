from flask import Blueprint, render_template, request
from flask_login import login_required

from app.core.auth.decorators import permission_required
from app.core.models.audit import AuditLog

audit_bp = Blueprint("audit", __name__)


@audit_bp.route("/")
@login_required
@permission_required("audit.view")
def log_list():
    page = request.args.get("page", 1, type=int)
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50)
    return render_template("audit/list.html", logs=logs)
