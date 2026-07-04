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
    from app.modules.scrape.service import list_user_papers, count_user_papers, count_all_notes
    from app.modules.academic.service import list_user_keywords
    from sqlalchemy import func
    from app.extensions import db
    from app.modules.academic.models import Keyword, UserKeyword

    for_you = list_user_papers(current_user, limit=10)
    user_keywords = list_user_keywords(current_user)
    has_interests = bool(user_keywords)
    user_keyword_values = {kw.value for kw in user_keywords}

    # Query trending keywords for all users to show recommendations
    top_keywords = (
        db.session.query(Keyword.value, func.count(UserKeyword.user_id).label("count"))
        .join(UserKeyword, Keyword.id == UserKeyword.keyword_id)
        .group_by(Keyword.value)
        .order_by(func.count(UserKeyword.user_id).desc())
        .limit(5)
        .all()
    )

    metrics = None
    recent_logs = None
    last_login_user = None
    user_stats = None

    if current_user.is_superuser:
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
        recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(5).all()
        last_login_user = (
            User.query.filter(User.deleted_at.is_(None), User.last_login_at.isnot(None))
            .order_by(User.last_login_at.desc())
            .first()
        )
    else:
        user_stats = {
            "interests_count": len(user_keywords),
            "favorites_count": count_user_papers(current_user, view="favorites"),
            "notes_count": count_all_notes(current_user),
        }

    return render_template(
        "dashboard/for_you.html",
        for_you=for_you,
        has_interests=has_interests,
        user_keywords=user_keywords,
        user_keyword_values=user_keyword_values,
        user_stats=user_stats,
        metrics=metrics,
        recent_logs=recent_logs,
        last_login_user=last_login_user,
        top_keywords=top_keywords,
    )


@dashboard_bp.route("/interests/add", methods=["POST"])
@login_required
def add_interest():
    from flask import request, redirect, url_for, flash
    from flask_babel import gettext as _
    from app.modules.academic.service import add_user_keyword
    from app.core.audit.middleware import log_action

    value = request.form.get("value", "").strip()
    if value:
        kw, err = add_user_keyword(current_user, value)
        if kw:
            log_action(
                "user.keyword_added",
                entity_type="keyword",
                entity_id=str(kw.id),
                changes={"value": kw.value},
            )
            flash(_("Interest added successfully."), "success")
        else:
            flash(_(err or "Failed to add interest."), "danger")
    else:
        flash(_("Interest name cannot be empty."), "danger")
    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/interests/<int:keyword_id>/delete", methods=["POST"])
@login_required
def delete_interest(keyword_id: int):
    from flask import redirect, url_for, flash
    from flask_babel import gettext as _
    from app.modules.academic.service import remove_user_keyword
    from app.core.audit.middleware import log_action

    ok, err = remove_user_keyword(current_user, keyword_id)
    if ok:
        log_action("user.keyword_removed", entity_type="keyword", entity_id=str(keyword_id))
        flash(_("Interest removed successfully."), "success")
    else:
        flash(_(err or "Failed to remove interest."), "danger")
    return redirect(url_for("dashboard.index"))


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
