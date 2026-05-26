from flask import flash, redirect, render_template, request, session, url_for
from flask_babel import _
from flask_login import current_user, login_required, login_user, logout_user

from app.core.audit.middleware import log_action
from app.core.auth import auth_bp
from app.core.auth.forms import (
    LoginForm,
    PasswordResetForm,
    PasswordResetRequestForm,
    RegisterForm,
    TotpCodeForm,
)
from app.core.auth.service import (
    make_password_reset_token,
    register_user,
    reset_password,
    verify_password_reset_token,
)
from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.auth.totp_service import consume_recovery_code, verify_totp
from app.core.models.user import User
from app.core.sessions.service import (
    clear_current_key,
    create_session,
    delete_session,
    get_current_key,
    set_current_key,
)
from app.extensions import db, limiter

PENDING_2FA_KEY = "pending_2fa_user_id"
PENDING_2FA_REMEMBER_KEY = "pending_2fa_remember"

_local = LocalAuthStrategy()


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        user = _local.authenticate({"username": form.username.data, "password": form.password.data})
        if user is None:
            flash(_("Invalid credentials or account locked."), "danger")
            return render_template("auth/login.html", form=form)

        # If 2FA is on, defer login until the user proves the second factor.
        # Store the bare minimum in the unauthenticated session: who we're
        # mid-authenticating + whether they ticked "remember me". The next
        # endpoint (/auth/2fa) burns this state on success or expiry.
        if user.is_totp_enabled:
            session[PENDING_2FA_KEY] = user.id
            session[PENDING_2FA_REMEMBER_KEY] = bool(form.remember_me.data)
            next_page = request.args.get("next")
            return redirect(
                url_for("auth.totp_challenge", next=next_page)
                if next_page
                else url_for("auth.totp_challenge")
            )

        login_user(user, remember=form.remember_me.data)
        key = create_session(user)
        set_current_key(key)
        log_action("user.login", entity_type="user", entity_id=user.id)
        next_page = request.args.get("next") or url_for("dashboard.index")
        return redirect(next_page)

    return render_template("auth/login.html", form=form)


@auth_bp.route("/2fa", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def totp_challenge():
    user_id = session.get(PENDING_2FA_KEY)
    if not user_id:
        return redirect(url_for("auth.login"))
    user = User.query.filter_by(id=user_id, deleted_at=None).first()
    if user is None or not user.is_totp_enabled:
        # Stale pending state — clear and bounce back to login.
        session.pop(PENDING_2FA_KEY, None)
        session.pop(PENDING_2FA_REMEMBER_KEY, None)
        return redirect(url_for("auth.login"))

    form = TotpCodeForm()
    if form.validate_on_submit():
        code = (form.code.data or "").strip()
        ok = verify_totp(user.totp_secret, code)
        used_recovery = False
        if not ok:
            # Fall back to recovery codes — same form field, looks like XXXX-XXXX
            # or 8+ characters and contains a dash, but we don't gate on shape;
            # consume_recovery_code returns False for anything that doesn't match.
            ok = consume_recovery_code(user, code)
            used_recovery = ok

        if not ok:
            log_action("user.totp_failed", entity_type="user", entity_id=user.id)
            flash(_("Code is invalid. Try again."), "danger")
            return render_template("auth/totp_challenge.html", form=form)

        remember = bool(session.pop(PENDING_2FA_REMEMBER_KEY, False))
        session.pop(PENDING_2FA_KEY, None)
        login_user(user, remember=remember)
        key = create_session(user)
        set_current_key(key)
        log_action(
            "user.login",
            entity_type="user",
            entity_id=user.id,
            changes={"second_factor": "recovery_code" if used_recovery else "totp"},
        )
        if used_recovery:
            flash(
                _("Logged in with a recovery code — consider regenerating your codes."), "warning"
            )
        next_page = request.args.get("next") or url_for("dashboard.index")
        return redirect(next_page)

    return render_template("auth/totp_challenge.html", form=form)


@auth_bp.route("/2fa/cancel", methods=["POST", "GET"])
def totp_cancel():
    session.pop(PENDING_2FA_KEY, None)
    session.pop(PENDING_2FA_REMEMBER_KEY, None)
    return redirect(url_for("auth.login"))


@auth_bp.route("/logout")
def logout():
    key = get_current_key()
    if key:
        delete_session(key)
    clear_current_key()
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    from app.core.settings.service import get_system_setting

    if not get_system_setting("registration_open", True):
        flash(_("Public registration is currently disabled."), "warning")
        return redirect(url_for("auth.login"))
    form = RegisterForm()
    if form.validate_on_submit():
        user, err = register_user(
            username=form.username.data,
            email=form.email.data,
            full_name=form.full_name.data,
            password=form.password.data,
        )
        if err:
            flash(_(err), "danger")
            return render_template("auth/register.html", form=form)
        log_action("user.register", entity_type="user", entity_id=user.id)
        flash(_("Account created. You can sign in now."), "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)


@auth_bp.route("/forgot", methods=["GET", "POST"])
@limiter.limit("3 per minute")
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    form = PasswordResetRequestForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter(User.email == email, User.deleted_at.is_(None)).first()
        # Always show the same flash to avoid email enumeration.
        if user and user.password_hash:
            token = make_password_reset_token(user)
            reset_url = url_for("auth.reset_password_view", token=token, _external=True)
            log_action("user.password_reset_requested", entity_type="user", entity_id=user.id)
            from app.core.email.service import send_password_reset

            sent, dev_url = send_password_reset(user, reset_url)
            if not sent and dev_url and current_app_is_debug():
                flash(_("Dev mode: reset link → %(url)s", url=dev_url), "info")
        flash(_("If that email exists, a reset link has been sent."), "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html", form=form)


@auth_bp.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def reset_password_view(token: str):
    user = verify_password_reset_token(token)
    if user is None:
        flash(_("Reset link is invalid or expired."), "danger")
        return redirect(url_for("auth.login"))
    form = PasswordResetForm()
    if form.validate_on_submit():
        reset_password(user, form.password.data)
        log_action("user.password_reset", entity_type="user", entity_id=user.id)
        flash(_("Password reset. You can sign in now."), "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", form=form)


def current_app_is_debug() -> bool:
    from flask import current_app

    return bool(current_app.debug)


@auth_bp.route("/oauth/<provider>")
def oauth_redirect(provider: str):
    """Login için OAuth yönlendirmesi."""
    from flask import session

    from app.extensions import oauth as _oauth

    client = getattr(_oauth, provider, None)
    if client is None:
        flash(_("Unknown OAuth provider."), "danger")
        return redirect(url_for("auth.login"))
    session.pop("_oauth_mode", None)  # login modu = mod yok
    redirect_uri = url_for("auth.oauth_callback", provider=provider, _external=True)
    return client.authorize_redirect(redirect_uri)


@auth_bp.route("/oauth/<provider>/link")
@login_required
def oauth_link_redirect(provider: str):
    """Giriş yapmış kullanıcı için OAuth hesabı bağlama yönlendirmesi."""
    from flask import session

    from app.extensions import oauth as _oauth

    client = getattr(_oauth, provider, None)
    if client is None:
        flash(_("Unknown OAuth provider."), "danger")
        return redirect(url_for("settings.profile", tab="oauth"))
    session["_oauth_mode"] = "link"
    session["_oauth_link_user_id"] = current_user.id
    redirect_uri = url_for("auth.oauth_callback", provider=provider, _external=True)
    return client.authorize_redirect(redirect_uri)


@auth_bp.route("/oauth/<provider>/callback")
def oauth_callback(provider: str):
    from flask import session

    from app.extensions import oauth as _oauth

    client = getattr(_oauth, provider, None)
    if client is None:
        flash(_("Unknown OAuth provider."), "danger")
        return redirect(url_for("auth.login"))

    token = client.authorize_access_token()
    userinfo = client.userinfo(token=token)

    # ── Link modu: giriş yapmış kullanıcı yeni hesap bağlıyor ──
    mode = session.pop("_oauth_mode", None)
    link_user_id = session.pop("_oauth_link_user_id", None)

    if mode == "link" and link_user_id and current_user.is_authenticated:
        _handle_oauth_link(
            user=current_user,
            provider=provider,
            provider_user_id=userinfo.get("sub", ""),
            email=userinfo.get("email", ""),
            raw_data=userinfo,
        )
        return redirect(url_for("settings.profile", tab="oauth"))

    # ── Login modu (mevcut davranış) ──
    from app.core.auth.strategies.oauth_base import resolve_oauth_user

    user = resolve_oauth_user(
        provider=provider,
        provider_user_id=userinfo.get("sub", ""),
        email=userinfo.get("email", ""),
        full_name=userinfo.get("name", ""),
        raw_data=userinfo,
    )
    if user is None:
        flash(_("OAuth login failed or registration disabled."), "danger")
        return redirect(url_for("auth.login"))

    login_user(user)
    key = create_session(user)
    set_current_key(key)
    return redirect(url_for("dashboard.index"))


def _handle_oauth_link(
    *, user, provider: str, provider_user_id: str, email: str, raw_data: dict
) -> None:
    """Mevcut kullanıcıya OAuth hesabı bağla — ya da zaten bağlıysa bildir."""
    from app.core.models.oauth_account import OAuthAccount

    existing = OAuthAccount.query.filter_by(
        provider=provider, provider_user_id=provider_user_id
    ).first()
    if existing:
        if existing.user_id == user.id:
            flash(
                _("This %(provider)s account is already linked.", provider=provider.capitalize()),
                "info",
            )
        else:
            flash(
                _(
                    "This %(provider)s account is linked to another user.",
                    provider=provider.capitalize(),
                ),
                "danger",
            )
        return

    account = OAuthAccount(
        user_id=user.id,
        provider=provider,
        provider_user_id=provider_user_id,
        email=email,
        raw_data=raw_data,
    )
    db.session.add(account)
    db.session.commit()
    log_action(
        "user.oauth_linked",
        entity_type="oauth_account",
        changes={"provider": provider, "email": email},
    )
    flash(_("%(provider)s account linked successfully.", provider=provider.capitalize()), "success")
