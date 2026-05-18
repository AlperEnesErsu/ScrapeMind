from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
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

TABS = [
    "personal",
    "email",
    "identifiers",
    "interests",
    "password",
    "prefs",
    "oauth",
    "account",
]


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


def _identifiers_ctx():
    from app.core.models.module import Module  # noqa: F401 — registry side-effect
    from app.modules.academic.forms import AddIdentifierForm
    from app.modules.academic.models import IdentifierType
    from app.modules.academic.service import list_user_identifiers

    form = AddIdentifierForm()
    types = (
        IdentifierType.query.filter(IdentifierType.deleted_at.is_(None))
        .order_by(IdentifierType.name)
        .all()
    )
    form.type_code.choices = [(t.code, t.name) for t in types] or [("email", "Email")]
    return {
        "form": form,
        "identifiers": list_user_identifiers(current_user),
    }


def _interests_ctx():
    from app.modules.academic.forms import AddKeywordForm
    from app.modules.academic.service import list_user_keywords

    return {"form": AddKeywordForm(), "keywords": list_user_keywords(current_user)}


_CTX_BUILDERS = {
    "personal": _personal_ctx,
    "email": _email_ctx,
    "identifiers": _identifiers_ctx,
    "interests": _interests_ctx,
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
    return _render_tab(
        "personal", form=form, flash_msg=_("Please correct the errors below."), flash_kind="danger"
    )


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
    return _render_tab(
        "email", form=form, flash_msg=_("Please correct the errors below."), flash_kind="danger"
    )


@settings_bp.route("/profile/password", methods=["POST"])
@login_required
def submit_password():
    form = PasswordChangeForm()
    if form.validate_on_submit():
        ok, err = change_password(current_user, form.current_password.data, form.new_password.data)
        if ok:
            log_action("user.change_password", entity_type="user", entity_id=current_user.id)
            return _render_tab(
                "password",
                form=PasswordChangeForm(),
                flash_msg=_("Password changed."),
                flash_kind="success",
            )
        return _render_tab("password", form=form, flash_msg=_(err), flash_kind="danger")
    return _render_tab(
        "password", form=form, flash_msg=_("Please correct the errors below."), flash_kind="danger"
    )


@settings_bp.route("/profile/identifiers/add", methods=["POST"])
@login_required
def submit_identifier_add():
    from app.modules.academic.forms import AddIdentifierForm
    from app.modules.academic.models import IdentifierType
    from app.modules.academic.service import add_identifier, make_email_verify_token

    form = AddIdentifierForm()
    types = IdentifierType.query.filter(IdentifierType.deleted_at.is_(None)).all()
    form.type_code.choices = [(t.code, t.name) for t in types]

    if form.validate_on_submit():
        ident, err = add_identifier(current_user, form.type_code.data, form.value.data)
        if err:
            return _render_tab(
                "identifiers", flash_msg=_(err), flash_kind="danger", **_identifiers_ctx()
            )
        log_action(
            "user.identifier_added",
            entity_type="user_identifier",
            entity_id=str(ident.id),
            changes={"type": form.type_code.data, "value": ident.value},
        )
        # For email type, offer verification link (dev-mode shows it in flash).
        if form.type_code.data == "email":
            token = make_email_verify_token(ident)
            verify_url = url_for("auth.verify_email", token=token, _external=True)
            if current_app_is_debug():
                return _render_tab(
                    "identifiers",
                    flash_msg=_("Dev mode: verification link → %(url)s", url=verify_url),
                    flash_kind="info",
                    **_identifiers_ctx(),
                )
            return _render_tab(
                "identifiers",
                flash_msg=_("Identifier added — a verification link has been sent."),
                flash_kind="success",
                **_identifiers_ctx(),
            )
        return _render_tab(
            "identifiers",
            flash_msg=_("Identifier added."),
            flash_kind="success",
            **_identifiers_ctx(),
        )
    return _render_tab(
        "identifiers",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
        **_identifiers_ctx(),
    )


@settings_bp.route("/profile/identifiers/<int:ident_id>/delete", methods=["POST"])
@login_required
def submit_identifier_delete(ident_id: int):
    from app.modules.academic.service import delete_identifier

    ok, err = delete_identifier(current_user, ident_id)
    if not ok:
        return _render_tab(
            "identifiers", flash_msg=_(err), flash_kind="danger", **_identifiers_ctx()
        )
    log_action("user.identifier_deleted", entity_type="user_identifier", entity_id=str(ident_id))
    return _render_tab(
        "identifiers",
        flash_msg=_("Identifier removed."),
        flash_kind="success",
        **_identifiers_ctx(),
    )


@settings_bp.route("/profile/identifiers/<int:ident_id>/resend", methods=["POST"])
@login_required
def submit_identifier_resend(ident_id: int):
    from app.modules.academic.models import UserIdentifier
    from app.modules.academic.service import make_email_verify_token

    ident = db.session.get(UserIdentifier, ident_id)
    if ident is None or ident.user_id != current_user.id or ident.is_verified:
        abort(404)
    # Only email type uses email_link verification right now.
    if ident.type.code != "email":
        abort(404)
    token = make_email_verify_token(ident)
    verify_url = url_for("auth.verify_email", token=token, _external=True)
    log_action(
        "user.identifier_verify_resent",
        entity_type="user_identifier",
        entity_id=str(ident_id),
    )
    if current_app_is_debug():
        return _render_tab(
            "identifiers",
            flash_msg=_("Dev mode: verification link → %(url)s", url=verify_url),
            flash_kind="info",
            **_identifiers_ctx(),
        )
    return _render_tab(
        "identifiers",
        flash_msg=_("Verification link re-sent."),
        flash_kind="success",
        **_identifiers_ctx(),
    )


@settings_bp.route("/profile/interests/add", methods=["POST"])
@login_required
def submit_keyword_add():
    from app.modules.academic.forms import AddKeywordForm
    from app.modules.academic.service import add_user_keyword

    form = AddKeywordForm()
    if form.validate_on_submit():
        kw, err = add_user_keyword(current_user, form.value.data)
        if err:
            return _render_tab(
                "interests", flash_msg=_(err), flash_kind="danger", **_interests_ctx()
            )
        log_action(
            "user.keyword_added",
            entity_type="keyword",
            entity_id=str(kw.id),
            changes={"value": kw.value},
        )
        return _render_tab(
            "interests",
            flash_msg=_("Keyword added."),
            flash_kind="success",
            **_interests_ctx(),
        )
    return _render_tab(
        "interests",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
        **_interests_ctx(),
    )


@settings_bp.route("/profile/interests/<int:keyword_id>/delete", methods=["POST"])
@login_required
def submit_keyword_delete(keyword_id: int):
    from app.modules.academic.service import remove_user_keyword

    ok, err = remove_user_keyword(current_user, keyword_id)
    if not ok:
        return _render_tab("interests", flash_msg=_(err), flash_kind="danger", **_interests_ctx())
    log_action("user.keyword_removed", entity_type="keyword", entity_id=str(keyword_id))
    return _render_tab(
        "interests",
        flash_msg=_("Keyword removed."),
        flash_kind="success",
        **_interests_ctx(),
    )


def current_app_is_debug() -> bool:
    from flask import current_app

    return bool(current_app.debug)


@settings_bp.route("/profile/prefs", methods=["POST"])
@login_required
def submit_prefs():
    form = PreferencesForm()
    if form.validate_on_submit():
        update_preferences(current_user, form.locale.data, form.timezone.data, form.theme.data)
        log_action(
            "user.update_prefs",
            entity_type="user",
            entity_id=current_user.id,
            changes={
                "locale": form.locale.data,
                "timezone": form.timezone.data,
                "theme": form.theme.data,
            },
        )
        ctx = _prefs_ctx()
        return _render_tab(
            "prefs", flash_msg=_("Preferences updated."), flash_kind="success", **ctx
        )
    return _render_tab(
        "prefs", form=form, flash_msg=_("Please correct the errors below."), flash_kind="danger"
    )


@settings_bp.route("/system", methods=["GET", "POST"])
@login_required
def system():
    # Inline guard so we can keep one route URL while gating it on perm.
    if not current_user.is_superuser:
        from app.core.rbac.service import user_has_permission

        if not user_has_permission(current_user, "system.manage"):
            abort(403)

    from app.core.settings.service import get_system_setting, set_system_setting
    from app.core.settings.system_forms import SystemSettingsForm

    form = SystemSettingsForm()
    if request.method == "GET":
        form.app_name.data = get_system_setting("app_name", "ScrapeMind")
        form.default_locale.data = get_system_setting("default_locale", "tr")
        form.oauth_auto_register.data = bool(get_system_setting("oauth_auto_register", False))
        form.registration_open.data = bool(get_system_setting("registration_open", True))

    if form.validate_on_submit():
        set_system_setting("app_name", form.app_name.data.strip(), updated_by_id=current_user.id)
        set_system_setting(
            "default_locale", form.default_locale.data, updated_by_id=current_user.id
        )
        set_system_setting(
            "oauth_auto_register", form.oauth_auto_register.data, updated_by_id=current_user.id
        )
        set_system_setting(
            "registration_open", form.registration_open.data, updated_by_id=current_user.id
        )
        log_action(
            "system_settings.update",
            entity_type="system_settings",
            entity_id=None,
            changes={
                "app_name": form.app_name.data,
                "default_locale": form.default_locale.data,
                "oauth_auto_register": form.oauth_auto_register.data,
                "registration_open": form.registration_open.data,
            },
        )
        flash(_("System settings saved."), "success")
        return redirect(url_for("settings.system"))

    return render_template("settings/system.html", form=form)


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
