from flask import Blueprint, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import or_

from app.core.models.menu import MenuItem
from app.core.models.role import Role
from app.core.models.user import User
from app.core.rbac.service import get_user_permissions

search_bp = Blueprint("search", __name__)


def _search_module_entities(q: str, like: str) -> tuple[list, list]:
    """Search entities owned by app/modules — papers + research keywords.

    core must not hard-depend on modules (flask-core-base rule #1), so the
    import is guarded: in a project built from the core template without the
    scrape/academic modules, global search simply skips these sections.
    """
    try:
        from app.extensions import db  # noqa: PLC0415
        from app.modules.academic.models import Keyword  # noqa: PLC0415
        from app.modules.scrape.models import Paper, UserPaper  # noqa: PLC0415
    except ImportError:
        return [], []

    # Papers matching title, abstract, matched keyword, or authors — scoped to
    # the current user's own feed (never other users' libraries).
    papers = (
        UserPaper.query.join(Paper)
        .filter(
            UserPaper.user_id == current_user.id,
            UserPaper.dismissed_at.is_(None),
            or_(
                Paper.title.ilike(like),
                Paper.abstract.ilike(like),
                UserPaper.matched_keyword.ilike(like),
                db.func.lower(db.cast(Paper.authors, db.String)).like(f"%{q.lower()}%"),
            ),
        )
        .order_by(Paper.published_at.desc())
        .limit(5)
        .all()
    )
    keywords = (
        Keyword.query.filter(Keyword.value.ilike(like)).order_by(Keyword.value).limit(5).all()
    )
    return papers, keywords


@search_bp.route("/search")
@login_required
def quick_search():
    q = (request.args.get("q") or "").strip()
    users: list = []
    roles: list = []
    menu_items: list = []
    papers: list = []
    keywords: list = []

    if len(q) >= 2:
        like = f"%{q}%"
        papers, keywords = _search_module_entities(q, like)

        if current_user.is_superuser:
            users = (
                User.query.filter(
                    User.deleted_at.is_(None),
                    or_(
                        User.username.ilike(like),
                        User.email.ilike(like),
                        User.full_name.ilike(like),
                    ),
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
                MenuItem.query.filter(
                    or_(MenuItem.code.ilike(like), MenuItem.label_key.ilike(like))
                )
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
        papers=papers,
        keywords=keywords,
        can_manage_users=can_manage_users,
        can_manage_roles=can_manage_roles,
        can_manage_menu=can_manage_menu,
    )
