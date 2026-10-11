from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.core.auth.decorators import permission_required
from app.core.rbac.forms import RoleForm
from app.core.rbac.service import (
    create_role,
    get_role,
    list_permissions_by_module,
    list_roles,
    permission_codes,
    soft_delete_role,
    ungrantable_codes,
    update_role,
)

rbac_bp = Blueprint("rbac", __name__)


def _refuse_ungrantable(codes) -> bool:
    """Flash and return True when `codes` holds a permission the current user
    lacks -- see `ungrantable_codes` for why a delegate is held to this."""
    missing = ungrantable_codes(current_user, codes)
    if missing:
        flash(
            _(
                "You cannot grant or revoke permissions you do not hold: %(codes)s",
                codes=", ".join(missing),
            ),
            "danger",
        )
        return True
    return False


@rbac_bp.route("/")
@login_required
@permission_required("roles.view")
def index():
    return redirect(url_for("rbac.role_list"))


@rbac_bp.route("/roles")
@login_required
@permission_required("roles.view")
def role_list():
    return render_template("rbac/role_list.html", roles=list_roles())


@rbac_bp.route("/roles/new", methods=["GET", "POST"])
@login_required
@permission_required("roles.manage")
def role_new():
    form = RoleForm()
    grouped_perms = list_permissions_by_module()
    if form.validate_on_submit():
        perm_ids = [int(pid) for pid in request.form.getlist("permission_ids")]
        if _refuse_ungrantable(permission_codes(perm_ids)):
            return redirect(url_for("rbac.role_list"))
        role = create_role(form.name.data, form.description.data, perm_ids)
        log_action(
            "role.create",
            entity_type="role",
            entity_id=role.id,
            changes={"name": role.name, "permissions": perm_ids},
        )
        flash(_("Role created."), "success")
        return redirect(url_for("rbac.role_list"))
    return render_template(
        "rbac/role_form.html",
        form=form,
        role=None,
        grouped_perms=grouped_perms,
        selected_perm_ids=set(),
    )


@rbac_bp.route("/roles/<int:role_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("roles.manage")
def role_edit(role_id: int):
    role = get_role(role_id)
    if role is None:
        abort(404)
    form = RoleForm(obj=role)
    grouped_perms = list_permissions_by_module()
    selected_perm_ids = {p.id for p in role.permissions}
    if form.validate_on_submit():
        perm_ids = [int(pid) for pid in request.form.getlist("permission_ids")]
        # Both directions count: removing a permission from a role demotes
        # everyone holding it, which is as much the delegate's to decide as
        # adding one.
        changed = permission_codes(set(perm_ids) ^ selected_perm_ids)
        if _refuse_ungrantable(changed):
            return redirect(url_for("rbac.role_edit", role_id=role.id))
        update_role(role, form.name.data, form.description.data, perm_ids)
        log_action(
            "role.update",
            entity_type="role",
            entity_id=role.id,
            changes={"name": role.name, "permissions": perm_ids},
        )
        flash(_("Role updated."), "success")
        return redirect(url_for("rbac.role_list"))
    return render_template(
        "rbac/role_form.html",
        form=form,
        role=role,
        grouped_perms=grouped_perms,
        selected_perm_ids=selected_perm_ids,
    )


@rbac_bp.route("/roles/<int:role_id>/delete", methods=["POST"])
@login_required
@permission_required("roles.manage")
def role_delete(role_id: int):
    role = get_role(role_id)
    if role is None:
        abort(404)
    if role.name == "admin":
        flash(_("The admin role cannot be deleted."), "danger")
        return redirect(url_for("rbac.role_list"))
    if _refuse_ungrantable({p.code for p in role.permissions}):
        return redirect(url_for("rbac.role_list"))
    soft_delete_role(role)
    log_action("role.delete", entity_type="role", entity_id=role.id, changes={"name": role.name})
    flash(_("Role deleted."), "success")
    return redirect(url_for("rbac.role_list"))


@rbac_bp.route("/permissions")
@login_required
@permission_required("permissions.view")
def permission_list():
    return render_template("rbac/permission_list.html", grouped_perms=list_permissions_by_module())
