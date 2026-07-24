from flask import render_template
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.auth.decorators import permission_required
from app.modules.dashboard import dashboard_bp


@dashboard_bp.route("/")
@login_required
def index():
    """Kişisel 'Senin için' sayfası — her kullanıcı görür.
    Admin kullanıcıları doğrudan admin overview sayfasına yönlendirilir.
    """
    if current_user.is_superuser:
        from flask import redirect, url_for

        return redirect(url_for("dashboard.admin_overview"))

    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func

    from app.extensions import db
    from app.modules.academic.models import Keyword, UserKeyword
    from app.modules.academic.service import list_user_keywords
    from app.modules.scrape.models import PaperNote, UserPaper
    from app.modules.scrape.service import count_all_notes, count_user_papers, list_user_papers

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

    user_stats = {
        "interests_count": len(user_keywords),
        "favorites_count": count_user_papers(current_user, view="favorites"),
        "read_later_count": count_user_papers(current_user, view="read_later"),
        "notes_count": count_all_notes(current_user),
    }

    # Dynamic onboarding stepper logic
    step1_done = len(user_keywords) > 0
    step2_done = count_user_papers(current_user, view="all") > 0
    step3_done = user_stats["favorites_count"] > 0 or user_stats["notes_count"] > 0
    onboarding_complete = step1_done and step2_done and step3_done

    # Weekly insights
    seven_days_ago = datetime.now(UTC) - timedelta(days=7)
    recent_papers_count = UserPaper.query.filter(
        UserPaper.user_id == current_user.id, UserPaper.created_at >= seven_days_ago
    ).count()
    recent_notes_count = (
        PaperNote.query.join(UserPaper)
        .filter(UserPaper.user_id == current_user.id, PaperNote.created_at >= seven_days_ago)
        .count()
    )

    top_keyword_row = (
        db.session.query(UserPaper.matched_keyword, func.count(UserPaper.id).label("count"))
        .filter(
            UserPaper.user_id == current_user.id,
            UserPaper.created_at >= seven_days_ago,
            UserPaper.matched_keyword.isnot(None),
        )
        .group_by(UserPaper.matched_keyword)
        .order_by(func.count(UserPaper.id).desc())
        .first()
    )
    active_interest = top_keyword_row[0] if top_keyword_row else None

    weekly_insights = {
        "recent_papers_count": recent_papers_count,
        "recent_notes_count": recent_notes_count,
        "active_interest": active_interest,
    }

    return render_template(
        "dashboard/for_you.html",
        for_you=for_you,
        has_interests=has_interests,
        user_keywords=user_keywords,
        user_keyword_values=user_keyword_values,
        user_stats=user_stats,
        step1_done=step1_done,
        step2_done=step2_done,
        step3_done=step3_done,
        onboarding_complete=onboarding_complete,
        weekly_insights=weekly_insights,
        top_keywords=top_keywords,
    )


@dashboard_bp.route("/interests/add", methods=["POST"])
@login_required
def add_interest():
    from flask import flash, redirect, request, url_for
    from flask_babel import gettext as _

    from app.core.audit.middleware import log_action
    from app.modules.academic.service import add_user_keyword

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
    from flask import flash, redirect, url_for
    from flask_babel import gettext as _

    from app.core.audit.middleware import log_action
    from app.modules.academic.service import remove_user_keyword

    ok, err = remove_user_keyword(current_user, keyword_id)
    if ok:
        log_action("user.keyword_removed", entity_type="keyword", entity_id=str(keyword_id))
        flash(_("Interest removed successfully."), "success")
    else:
        flash(_(err or "Failed to remove interest."), "danger")
    return redirect(url_for("dashboard.index"))


ACTION_LABELS = {
    "auth.login": "Giriş Yapıldı",
    "auth.logout": "Çıkış Yapıldı",
    "auth.register": "Yeni Kayıt",
    "paper.favorite_toggled": "Favori Durumu Değiştirildi",
    "paper.dismissed": "Makale Gizlendi",
    "paper.undismissed": "Makale Gizlemesi Geri Alındı",
    "paper.note_added": "Not Eklendi",
    "paper.note_deleted": "Not Silindi",
    "paper.note_edited": "Not Düzenlendi",
    "paper.analysis_generated": "AI Analiz Üretildi",
    "paper.translation_generated": "Türkçe Çeviri Üretildi",
    "paper.chat_message_sent": "Yapay Zekaya Soru Soruldu",
    "scrape.manual_run": "Taramayı Tetikledi",
    "user.keyword_added": "İlgi Alanı Eklendi",
    "user.keyword_removed": "İlgi Alanı Kaldırıldı",
}


@dashboard_bp.route("/admin/overview")
@login_required
@permission_required("dashboard.admin")
def admin_overview():
    """Admin dashboard — sistem metrikleri ve son aktivite."""
    from datetime import UTC, datetime, timedelta

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

    # Calculate Trend indicators (A4)
    now = datetime.now(UTC)
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)

    new_users_last_7 = User.query.filter(
        User.created_at >= seven_days_ago, User.deleted_at.is_(None)
    ).count()
    new_users_prev_7 = User.query.filter(
        User.created_at >= fourteen_days_ago,
        User.created_at < seven_days_ago,
        User.deleted_at.is_(None),
    ).count()
    user_trend = new_users_last_7 - new_users_prev_7

    logs_last_7 = AuditLog.query.filter(AuditLog.created_at >= seven_days_ago).count()
    logs_prev_7 = AuditLog.query.filter(
        AuditLog.created_at >= fourteen_days_ago, AuditLog.created_at < seven_days_ago
    ).count()
    logs_trend = logs_last_7 - logs_prev_7

    # Check Celery and Redis Health status (A5)
    celery_status = "unconfigured"
    redis_status = "disconnected"
    try:
        from celery import current_app

        with current_app.connection_for_write() as conn:
            conn.connect()
            redis_status = "connected"
    except Exception:
        redis_status = "disconnected"

    try:
        from celery import current_app

        inspect = current_app.control.inspect(timeout=0.3)
        pings = inspect.ping() if inspect else None
        if pings:
            celery_status = f"active ({len(pings)} worker)"
        else:
            celery_status = "no workers active"
    except Exception:
        celery_status = "offline"

    health_status = {"redis": redis_status, "celery": celery_status, "db": "connected"}

    return render_template(
        "dashboard/admin_overview.html",
        metrics=metrics,
        recent_logs=recent_logs,
        last_login_user=last_login_user,
        top_keywords=top_keywords,
        new_users_last_7=new_users_last_7,
        user_trend=user_trend,
        logs_last_7=logs_last_7,
        logs_trend=logs_trend,
        health_status=health_status,
        action_labels=ACTION_LABELS,
    )


@dashboard_bp.route("/notifications")
@login_required
def list_notifications():
    """Retrieve notifications for the current user."""
    from app.core.models.notification import Notification
    from app.extensions import db

    notifications = (
        Notification.query.filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(10)
        .all()
    )

    for n in notifications:
        n.is_read = True
    db.session.commit()

    return render_template("core/_notifications_dropdown.html", notifications=notifications)


@dashboard_bp.route("/notifications/all")
@login_required
def all_notifications():
    """Full, paginated notification history — the dropdown only shows 10.

    Opening the page does NOT mark everything read (unlike the dropdown); the
    user marks read explicitly, so the unread badge survives a glance.
    """
    from flask import request

    from app.core.models.notification import Notification

    page = request.args.get("page", 1, type=int)
    notifications = (
        Notification.query.filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .paginate(page=page, per_page=20, error_out=False)
    )
    unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return render_template(
        "core/notifications_all.html", notifications=notifications, unread=unread
    )


@dashboard_bp.route("/notifications/read-all", methods=["POST"])
@login_required
def mark_all_notifications_read():
    """Mark every notification for this user as read. Bulk UPDATE, no row load."""
    from flask import flash, redirect, url_for

    from app.core.models.notification import Notification
    from app.extensions import db

    updated = Notification.query.filter_by(user_id=current_user.id, is_read=False).update(
        {Notification.is_read: True}, synchronize_session=False
    )
    db.session.commit()
    flash(_("Marked %(n)s notifications as read.", n=updated), "success")
    return redirect(url_for("dashboard.all_notifications"))
