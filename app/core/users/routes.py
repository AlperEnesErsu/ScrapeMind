from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.core.auth.decorators import permission_required
from app.core.models.role import Role
from app.core.users.forms import UserEditForm
from app.core.users.service import (
    get_user,
    list_users,
    lock_user,
    set_user_roles,
    soft_delete_user,
    unlock_user,
    update_user,
)

users_bp = Blueprint("users", __name__)


@users_bp.route("/")
@login_required
@permission_required("users.view")
def user_list():
    q = request.args.get("q", "").strip()
    only_active = request.args.get("only_active") == "1"
    users = list_users(query=q or None, only_active=only_active)
    return render_template("users/list.html", users=users, q=q, only_active=only_active)


@users_bp.route("/<int:user_id>", methods=["GET", "POST"])
@login_required
@permission_required("users.manage")
def user_edit(user_id: int):
    user = get_user(user_id)
    if user is None:
        abort(404)
    form = UserEditForm(obj=user)
    all_roles = Role.query.filter(Role.deleted_at.is_(None)).order_by(Role.name).all()
    selected_role_ids = {r.id for r in user.roles}

    if form.validate_on_submit():
        # Protect against admin demoting themselves
        is_superuser = form.is_superuser.data
        if user.id == current_user.id and not is_superuser and current_user.is_superuser:
            flash(_("You cannot remove your own superuser flag."), "danger")
            return redirect(url_for("users.user_edit", user_id=user.id))

        ok, err = update_user(
            user,
            full_name=form.full_name.data,
            email=form.email.data,
            is_active=form.is_active.data,
            is_superuser=is_superuser,
            avatar_url=form.avatar_url.data,
        )
        if not ok:
            flash(_(err), "danger")
        else:
            role_ids = [int(rid) for rid in request.form.getlist("role_ids")]
            set_user_roles(user, role_ids)
            log_action(
                "user.update",
                entity_type="user",
                entity_id=str(user.id),
                changes={
                    "email": user.email,
                    "is_active": user.is_active,
                    "is_superuser": user.is_superuser,
                    "role_ids": role_ids,
                },
            )
            flash(_("User updated."), "success")
            return redirect(url_for("users.user_list"))

    return render_template(
        "users/edit.html",
        form=form,
        user=user,
        all_roles=all_roles,
        selected_role_ids=selected_role_ids,
    )


@users_bp.route("/<int:user_id>/lock", methods=["POST"])
@login_required
@permission_required("users.manage")
def user_lock(user_id: int):
    user = get_user(user_id)
    if user is None:
        abort(404)
    if user.id == current_user.id:
        flash(_("You cannot lock your own account."), "danger")
        return redirect(url_for("users.user_list"))
    lock_user(user)
    log_action("user.lock", entity_type="user", entity_id=str(user.id))
    flash(_("User locked."), "success")
    return redirect(url_for("users.user_list"))


@users_bp.route("/<int:user_id>/unlock", methods=["POST"])
@login_required
@permission_required("users.manage")
def user_unlock(user_id: int):
    user = get_user(user_id)
    if user is None:
        abort(404)
    unlock_user(user)
    log_action("user.unlock", entity_type="user", entity_id=str(user.id))
    flash(_("User unlocked."), "success")
    return redirect(url_for("users.user_list"))


@users_bp.route("/<int:user_id>/delete", methods=["POST"])
@login_required
@permission_required("users.manage")
def user_delete(user_id: int):
    user = get_user(user_id)
    if user is None:
        abort(404)
    if user.id == current_user.id:
        flash(_("You cannot delete your own account."), "danger")
        return redirect(url_for("users.user_list"))
    if user.username == "admin":
        flash(_("The admin user cannot be deleted."), "danger")
        return redirect(url_for("users.user_list"))
    soft_delete_user(user)
    log_action("user.delete", entity_type="user", entity_id=str(user.id))
    flash(_("User deleted."), "success")
    return redirect(url_for("users.user_list"))
