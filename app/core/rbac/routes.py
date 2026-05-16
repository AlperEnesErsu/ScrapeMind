from flask import Blueprint, render_template
from flask_login import login_required

from app.core.auth.decorators import permission_required
from app.core.models.role import Role

rbac_bp = Blueprint("rbac", __name__)


@rbac_bp.route("/roles")
@login_required
@permission_required("roles.view")
def role_list():
    roles = Role.query.filter(Role.deleted_at.is_(None)).all()
    return render_template("rbac/role_list.html", roles=roles)
