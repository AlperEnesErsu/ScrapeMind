from flask import Blueprint, abort, jsonify, render_template, request
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.core.settings.forms import (
    EmailChangeForm,
    PasswordChangeForm,
    PersonalInfoForm,
    PreferencesForm,
)
from app.core.settings.service import (
    change_password,
    get_theme,
    update_email,
    update_personal_info,
    update_preferences,
)
from app.extensions import db

settings_bp = Blueprint("settings", __name__)

TABS = ["personal", "email", "password", "prefs", "oauth", "account"]


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _render_tab(tab: str, **ctx):
    template = f"settings/_tab_{tab}.html"
    return render_template(template, active_tab=tab, **ctx)


def _personal_ctx():
    form = PersonalInfoForm(obj=current_user)
    return {"form": form}


def _email_ctx():
    form = EmailChangeForm()
    if request.method == "GET":
        form.email.data = current_user.email
    return {"form": form}


def _password_ctx():
    return {"form": PasswordChangeForm()}


def _prefs_ctx():
    form = PreferencesForm()
    if request.method == "GET":
        form.locale.data = current_user.locale
        form.timezone.data = current_user.timezone
        form.theme.data = get_theme(current_user)
    return {"form": form}


def _oauth_ctx():
    return {"accounts": current_user.oauth_accounts}


def _account_ctx():
    return {"user": current_user}


_CTX_BUILDERS = {
    "personal": _personal_ctx,
    "email": _email_ctx,
    "password": _password_ctx,
    "prefs": _prefs_ctx,
    "oauth": _oauth_ctx,
    "account": _account_ctx,
}


@settings_bp.route("/profile")
@login_required
def profile():
    tab = request.args.get("tab", "personal")
    if tab not in TABS:
        tab = "personal"
    ctx = _CTX_BUILDERS[tab]()
    return render_template("settings/profile.html", active_tab=tab, **ctx)


@settings_bp.route("/profile/tabs/<tab>", methods=["GET"])
@login_required
def profile_tab(tab: str):
    if tab not in TABS:
        abort(404)
    ctx = _CTX_BUILDERS[tab]()
    return _render_tab(tab, **ctx)


@settings_bp.route("/profile/personal", methods=["POST"])
@login_required
def submit_personal():
    form = PersonalInfoForm()
    if form.validate_on_submit():
        update_personal_info(current_user, form.full_name.data, form.avatar_url.data)
        log_action("user.update_personal", entity_type="user", entity_id=current_user.id)
        ctx = _personal_ctx()
        return _render_tab("personal", flash_msg=_("Profile updated."), flash_kind="success", **ctx)
    return _render_tab("personal", form=form, flash_msg=_("Please correct the errors below."), flash_kind="danger")


@settings_bp.route("/profile/email", methods=["POST"])
@login_required
def submit_email():
    form = EmailChangeForm()
    if form.validate_on_submit():
        ok, err = update_email(current_user, form.email.data, form.current_password.data)
        if ok:
            log_action("user.update_email", entity_type="user", entity_id=current_user.id)
            ctx = _email_ctx()
            return _render_tab("email", flash_msg=_("Email updated."), flash_kind="success", **ctx)
        return _render_tab("email", form=form, flash_msg=_(err), flash_kind="danger")
    return _render_tab("email", form=form, flash_msg=_("Please correct the errors below."), flash_kind="danger")


@settings_bp.route("/profile/password", methods=["POST"])
@login_required
def submit_password():
    form = PasswordChangeForm()
    if form.validate_on_submit():
        ok, err = change_password(current_user, form.current_password.data, form.new_password.data)
        if ok:
            log_action("user.change_password", entity_type="user", entity_id=current_user.id)
            return _render_tab("password", form=PasswordChangeForm(),
                               flash_msg=_("Password changed."), flash_kind="success")
        return _render_tab("password", form=form, flash_msg=_(err), flash_kind="danger")
    return _render_tab("password", form=form, flash_msg=_("Please correct the errors below."), flash_kind="danger")


@settings_bp.route("/profile/prefs", methods=["POST"])
@login_required
def submit_prefs():
    form = PreferencesForm()
    if form.validate_on_submit():
        update_preferences(current_user, form.locale.data, form.timezone.data, form.theme.data)
        log_action("user.update_prefs", entity_type="user", entity_id=current_user.id,
                   changes={"locale": form.locale.data, "timezone": form.timezone.data, "theme": form.theme.data})
        ctx = _prefs_ctx()
        return _render_tab("prefs", flash_msg=_("Preferences updated."), flash_kind="success", **ctx)
    return _render_tab("prefs", form=form, flash_msg=_("Please correct the errors below."), flash_kind="danger")


@settings_bp.route("/system")
@login_required
def system():
    return render_template("settings/system.html")


@settings_bp.route("/theme", methods=["POST"])
@login_required
def set_theme():
    """Legacy HTMX/fetch endpoint — kept for topbar quick-toggle compatibility."""
    from app.core.models.settings import UserSettings

    data = request.get_json(silent=True) or {}
    theme = data.get("theme", "light")
    if theme not in ("light", "dark"):
        return jsonify({"error": "invalid theme"}), 400
    user_settings = current_user.settings
    if user_settings is None:
        user_settings = UserSettings(user_id=current_user.id, settings={})
        db.session.add(user_settings)
    settings_copy = dict(user_settings.settings or {})
    settings_copy["theme"] = theme
    user_settings.settings = settings_copy
    db.session.commit()
    return jsonify({"theme": theme})
