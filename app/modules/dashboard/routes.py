from flask import render_template
from flask_login import current_user, login_required

from app.core.auth.decorators import permission_required
from app.modules.dashboard import dashboard_bp


@dashboard_bp.route("/")
@login_required
def index():
    """Kişisel 'Senin için' sayfası — her kullanıcı görür."""
    from app.modules.scrape.service import list_user_papers

    for_you = list_user_papers(current_user, limit=10)
    has_interests = bool(getattr(current_user, "keyword_links", None))
    return render_template(
        "dashboard/for_you.html",
        for_you=for_you,
        has_interests=has_interests,
    )


@dashboard_bp.route("/admin/overview")
@login_required
@permission_required("dashboard.admin")
def admin_overview():
    """Admin dashboard — sistem metrikleri ve son aktivite."""
    from app.core.models.audit import AuditLog
    from app.core.models.menu import MenuItem
    from app.core.models.permission import Permission
    from app.core.models.role import Role
    from app.core.models.user import User

    metrics = {
        "users_total":       User.query.filter(User.deleted_at.is_(None)).count(),
        "users_active":      User.query.filter(User.deleted_at.is_(None), User.is_active.is_(True)).count(),
        "users_locked":      User.query.filter(User.deleted_at.is_(None), User.is_locked.is_(True)).count(),
        "roles_total":       Role.query.filter(Role.deleted_at.is_(None)).count(),
        "permissions_total": Permission.query.count(),
        "menu_items_total":  MenuItem.query.count(),
    }
    recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(10).all()
    last_login_user = (
        User.query.filter(User.deleted_at.is_(None), User.last_login_at.isnot(None))
        .order_by(User.last_login_at.desc())
        .first()
    )
    return render_template(
        "dashboard/admin_overview.html",
        metrics=metrics,
        recent_logs=recent_logs,
        last_login_user=last_login_user,
    )
