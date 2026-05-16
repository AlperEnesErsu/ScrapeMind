from flask import flash, redirect, render_template, request, url_for
from flask_babel import _
from flask_login import current_user, login_user, logout_user

from app.core.auth import auth_bp
from app.core.auth.forms import LoginForm
from app.core.auth.strategies.local import LocalAuthStrategy
from app.extensions import limiter

_local = LocalAuthStrategy()


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        user = _local.authenticate(
            {"username": form.username.data, "password": form.password.data}
        )
        if user is None:
            flash(_("Invalid credentials or account locked."), "danger")
            return render_template("auth/login.html", form=form)

        login_user(user, remember=form.remember_me.data)
        next_page = request.args.get("next") or url_for("dashboard.index")
        return redirect(next_page)

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/oauth/<provider>")
def oauth_redirect(provider: str):
    from app.extensions import oauth as _oauth

    client = getattr(_oauth, provider, None)
    if client is None:
        flash(_("Unknown OAuth provider."), "danger")
        return redirect(url_for("auth.login"))
    redirect_uri = url_for("auth.oauth_callback", provider=provider, _external=True)
    return client.authorize_redirect(redirect_uri)


@auth_bp.route("/oauth/<provider>/callback")
def oauth_callback(provider: str):
    from app.core.auth.strategies.oauth_base import resolve_oauth_user
    from app.extensions import oauth as _oauth

    client = getattr(_oauth, provider, None)
    if client is None:
        flash(_("Unknown OAuth provider."), "danger")
        return redirect(url_for("auth.login"))

    token = client.authorize_access_token()
    userinfo = client.userinfo(token=token)

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
    return redirect(url_for("dashboard.index"))
