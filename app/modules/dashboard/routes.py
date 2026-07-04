from flask import render_template
from flask_login import current_user, login_required

from app.core.auth.decorators import permission_required
from app.modules.dashboard import dashboard_bp


@dashboard_bp.route("/")
@login_required
def index():
    """Kişisel 'Senin için' sayfası — her kullanıcı görür.
    Admin kullanıcıları için ek olarak sistem metrikleri ve yönetim alanları yüklenir.
    """
    from app.modules.scrape.service import list_user_papers

    for_you = list_user_papers(current_user, limit=10)
    has_interests = bool(getattr(current_user, "keyword_links", None))

    metrics = None
    recent_logs = None
    last_login_user = None
    top_keywords = None

    if current_user.is_superuser:
        from app.core.models.audit import AuditLog
        from app.core.models.menu import MenuItem
        from app.core.models.permission import Permission
        from app.core.models.role import Role
        from app.core.models.user import User
        from sqlalchemy import func
        from app.extensions import db
        from app.modules.academic.models import Keyword, UserKeyword

        metrics = {
            "users_total": User.query.filter(User.deleted_at.is_(None)).count(),
            "users_active": User.query.filter(
                User.deleted_at.is_(None), User.is_active.is_(True)
            ).count(),
            "users_locked": User.query.filter(
                User.deleted_at.is_(None), User.is_locked.is_(True)
            ).count(),
            "roles_total": Role.query.filter(Role.deleted_at.is_(None)).count(),
            "permissions_total": Permission.query.count(),
            "menu_items_total": MenuItem.query.count(),
        }
        recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(5).all()
        last_login_user = (
            User.query.filter(User.deleted_at.is_(None), User.last_login_at.isnot(None))
            .order_by(User.last_login_at.desc())
            .first()
        )
        top_keywords = (
            db.session.query(Keyword.value, func.count(UserKeyword.user_id).label("count"))
            .join(UserKeyword, Keyword.id == UserKeyword.keyword_id)
            .group_by(Keyword.value)
            .order_by(func.count(UserKeyword.user_id).desc())
            .limit(5)
            .all()
        )

    return render_template(
        "dashboard/for_you.html",
        for_you=for_you,
        has_interests=has_interests,
        metrics=metrics,
        recent_logs=recent_logs,
        last_login_user=last_login_user,
        top_keywords=top_keywords,
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
        "users_total": User.query.filter(User.deleted_at.is_(None)).count(),
        "users_active": User.query.filter(
            User.deleted_at.is_(None), User.is_active.is_(True)
        ).count(),
        "users_locked": User.query.filter(
            User.deleted_at.is_(None), User.is_locked.is_(True)
        ).count(),
        "roles_total": Role.query.filter(Role.deleted_at.is_(None)).count(),
        "permissions_total": Permission.query.count(),
        "menu_items_total": MenuItem.query.count(),
    }
    recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(10).all()
    last_login_user = (
        User.query.filter(User.deleted_at.is_(None), User.last_login_at.isnot(None))
        .order_by(User.last_login_at.desc())
        .first()
    )

    from sqlalchemy import func
    from app.extensions import db
    from app.modules.academic.models import Keyword, UserKeyword

    top_keywords = (
        db.session.query(Keyword.value, func.count(UserKeyword.user_id).label("count"))
        .join(UserKeyword, Keyword.id == UserKeyword.keyword_id)
        .group_by(Keyword.value)
        .order_by(func.count(UserKeyword.user_id).desc())
        .limit(5)
        .all()
    )

    return render_template(
        "dashboard/admin_overview.html",
        metrics=metrics,
        recent_logs=recent_logs,
        last_login_user=last_login_user,
        top_keywords=top_keywords,
    )
