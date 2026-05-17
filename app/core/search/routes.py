from flask import Blueprint, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import or_

from app.core.models.menu import MenuItem
from app.core.models.role import Role
from app.core.models.user import User
from app.core.rbac.service import get_user_permissions

search_bp = Blueprint("search", __name__)


@search_bp.route("/search")
@login_required
def quick_search():
    q = (request.args.get("q") or "").strip()
    users: list = []
    roles: list = []
    menu_items: list = []
    if len(q) >= 2:
        like = f"%{q}%"
        users = (
            User.query.filter(
                User.deleted_at.is_(None),
                or_(User.username.ilike(like), User.email.ilike(like), User.full_name.ilike(like)),
            )
            .order_by(User.username)
            .limit(5)
            .all()
        )
        roles = (
            Role.query.filter(Role.deleted_at.is_(None), Role.name.ilike(like))
            .order_by(Role.name)
            .limit(5)
            .all()
        )
        menu_items = (
            MenuItem.query.filter(or_(MenuItem.code.ilike(like), MenuItem.label_key.ilike(like)))
            .order_by(MenuItem.code)
            .limit(5)
            .all()
        )

    perms = frozenset() if current_user.is_superuser else get_user_permissions(current_user)
    can_manage_users = current_user.is_superuser or "users.manage" in perms
    can_manage_roles = current_user.is_superuser or "roles.manage" in perms
    can_manage_menu = current_user.is_superuser or "menu.manage" in perms

    return render_template(
        "core/_search_results.html",
        q=q,
        users=users,
        roles=roles,
        menu_items=menu_items,
        can_manage_users=can_manage_users,
        can_manage_roles=can_manage_roles,
        can_manage_menu=can_manage_menu,
    )
