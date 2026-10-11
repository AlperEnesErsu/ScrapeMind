from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.core.auth.decorators import permission_required
from app.core.models.role import Role
from app.core.rbac.service import role_permission_codes, ungrantable_codes
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

# What `users.manage` does *not* include, for anyone who is not a superuser:
# the superuser flag, a superuser's account, and roles carrying permissions
# the actor lacks. Each of those turned "may manage users" into "is a
# superuser" -- see rbac.service.ungrantable_codes.


def _require_superuser_to_touch(user) -> None:
    """403 when a non-superuser acts on a superuser's account."""
    if user.is_superuser and not current_user.is_superuser:
        abort(403)


def _refuse_ungrantable_roles(role_ids) -> bool:
    missing = ungrantable_codes(current_user, role_permission_codes(role_ids))
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


@users_bp.route("/")
@login_required
@permission_required("users.view")
def user_list():
    q = request.args.get("q", "").strip()
    only_active = request.args.get("only_active") == "1"
    users = list_users(query=q or None, only_active=only_active)
    return render_template("users/list.html", users=users, q=q, only_active=only_active)


@users_bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("users.manage")
def user_new():
    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.user import User
    from app.core.users.forms import UserCreateForm
    from app.extensions import db

    form = UserCreateForm()
    all_roles = Role.query.filter(Role.deleted_at.is_(None)).order_by(Role.name).all()

    if form.validate_on_submit():
        username = form.username.data.strip()
        email = form.email.data.strip().lower()

        role_ids = [int(rid) for rid in request.form.getlist("role_ids")]
        # What the actor may hand out first, then the unique constraints.
        if form.is_superuser.data and not current_user.is_superuser:
            form.is_superuser.errors.append(
                _("Only a superuser can grant or revoke superuser status.")
            )
        elif _refuse_ungrantable_roles(role_ids):
            pass  # flashed; re-render the form with what was typed
        elif User.query.filter(User.username == username).first():
            form.username.errors.append(_("Username already in use."))
        elif User.query.filter(User.email == email).first():
            form.email.errors.append(_("Email already in use."))
        else:
            # Create user
            user = User(
                username=username,
                email=email,
                full_name=form.full_name.data.strip(),
                password_hash=LocalAuthStrategy.hash_password(form.password.data),
                is_active=form.is_active.data,
                is_superuser=form.is_superuser.data,
                avatar_url=(form.avatar_url.data or "").strip() or None,
            )
            db.session.add(user)
            db.session.flush()

            # Set roles
            set_user_roles(user, role_ids)

            log_action(
                "user.create",
                entity_type="user",
                entity_id=str(user.id),
                changes={
                    "username": user.username,
                    "email": user.email,
                    "is_active": user.is_active,
                    "is_superuser": user.is_superuser,
                    "role_ids": role_ids,
                },
            )
            flash(_("User created."), "success")
            return redirect(url_for("users.user_list"))

    return render_template(
        "users/create.html",
        form=form,
        all_roles=all_roles,
    )


@users_bp.route("/<int:user_id>", methods=["GET", "POST"])
@login_required
@permission_required("users.manage")
def user_edit(user_id: int):
    user = get_user(user_id)
    if user is None:
        abort(404)
    _require_superuser_to_touch(user)
    form = UserEditForm(obj=user)
    all_roles = Role.query.filter(Role.deleted_at.is_(None)).order_by(Role.name).all()
    selected_role_ids = {r.id for r in user.roles}

    if form.validate_on_submit():
        # Protect against admin demoting themselves
        is_superuser = form.is_superuser.data
        if user.id == current_user.id and not is_superuser and current_user.is_superuser:
            flash(_("You cannot remove your own superuser flag."), "danger")
            return redirect(url_for("users.user_edit", user_id=user.id))
        if is_superuser != user.is_superuser and not current_user.is_superuser:
            flash(_("Only a superuser can grant or revoke superuser status."), "danger")
            return redirect(url_for("users.user_edit", user_id=user.id))
        role_ids = [int(rid) for rid in request.form.getlist("role_ids")]
        # Adding a role and taking one away are both grants of a kind.
        if _refuse_ungrantable_roles(set(role_ids) ^ selected_role_ids):
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

    # Academic profile (read-only here — only the user themselves can edit)
    from app.modules.academic.service import list_user_identifiers, list_user_keywords

    identifiers = list_user_identifiers(user)
    keywords = list_user_keywords(user)

    return render_template(
        "users/edit.html",
        form=form,
        user=user,
        all_roles=all_roles,
        selected_role_ids=selected_role_ids,
        identifiers=identifiers,
        keywords=keywords,
    )


@users_bp.route("/<int:user_id>/lock", methods=["POST"])
@login_required
@permission_required("users.manage")
def user_lock(user_id: int):
    user = get_user(user_id)
    if user is None:
        abort(404)
    _require_superuser_to_touch(user)
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
    _require_superuser_to_touch(user)
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
    _require_superuser_to_touch(user)
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
