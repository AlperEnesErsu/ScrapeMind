import structlog
from flask import Flask, has_request_context

from app.config import get_config
from app.extensions import babel, csrf, db, limiter, login_manager, mail, migrate, oauth

logger = structlog.get_logger()


def create_app() -> Flask:
    import app.core.models  # noqa: F401 — registers all SQLAlchemy models before init
    from app.modules import discover_and_register_models

    discover_and_register_models()

    app = Flask(__name__, template_folder="core/templates", static_folder="core/static")
    app.config.from_object(get_config())
    _validate_production_config(app)

    from app.core.audit.labels import humanize_action
    from app.core.ui.splash import pop_splash

    app.jinja_env.globals["humanize_action"] = humanize_action
    # Reads *and clears* a session flag, so it belongs to `base.html` alone --
    # see app/core/ui/splash.py for why the pop lives in the template.
    app.jinja_env.globals["pop_splash"] = pop_splash

    _apply_proxy_fix(app)
    _init_extensions(app)
    _init_logging(app)
    _init_observability(app)
    _init_celery(app)
    _register_blueprints(app)
    _register_session_guard(app)
    _register_context_processors(app)
    _register_error_handlers(app)
    _register_security_headers(app)

    # Plugin discovery runs AFTER extensions so db is ready.
    # Migrations must have run before this point — see wsgi.py / entrypoint.sh.
    # During `flask db init/migrate` the tables don't exist yet; we skip silently.
    with app.app_context():
        from sqlalchemy import inspect as sa_inspect

        from app.modules import discover_and_sync_modules

        inspector = sa_inspect(db.engine)
        if inspector.has_table("modules"):
            discover_and_sync_modules()
        else:
            logger.warning(
                "plugin_discovery_skipped",
                reason="modules table not found — run flask db upgrade first",
            )

    return app


def _validate_production_config(app: Flask) -> None:
    """Fail fast in production if security-critical config is empty.

    The configs deliberately fall back to empty strings so importing the
    module during CI/alembic doesn't crash. That's fine for dev/test, but a
    production process booting with an empty SECRET_KEY (forgeable sessions)
    or DATABASE_URL must not start silently — refuse loudly instead.
    """
    import os

    if os.getenv("FLASK_ENV") != "production":
        return
    missing = []
    if not app.config.get("SECRET_KEY"):
        missing.append("SECRET_KEY")
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        missing.append("DATABASE_URL")
    if missing:
        raise RuntimeError(
            "Production start-up aborted — missing required config: "
            f"{', '.join(missing)}. Set them in the environment before launching."
        )

    # In-memory rate limiting is per *process*, and production runs gunicorn
    # with four workers plus separate worker and beat containers. Every limit
    # silently becomes four times looser and resets on each restart -- and the
    # endpoint that matters is the login form.
    #
    # This refuses to boot rather than warning, for the same reason the checks
    # above do: a warning in a start-up log is a thing nobody reads until they
    # are already looking for why the brute-force protection did not hold.
    storage = str(app.config.get("RATELIMIT_STORAGE_URI") or "")
    if app.config.get("RATELIMIT_ENABLED", True) and storage.startswith("memory:"):
        raise RuntimeError(
            "Production start-up aborted — RATELIMIT_STORAGE_URI is in-memory "
            f"({storage!r}), which counts per process and resets on restart. "
            "Point it at the Redis already in the stack, e.g. "
            "RATELIMIT_STORAGE_URI=redis://redis:6379/1"
        )


def _register_oauth_providers(app: Flask) -> list[str]:
    """Register the OAuth clients that are configured. Returns their names.

    Nothing called `register()` before this. The strategy classes defining it
    were imported by nothing, so authlib held no clients, `getattr(oauth,
    "google", None)` returned None, and every OAuth login ended at "Unknown
    OAuth provider" -- while the login page offered both buttons. The feature
    had never worked.

    Registration is conditional on the credentials being present, which also
    settles what to do about the buttons without anyone having to decide
    whether this project wants OAuth: set the environment variables and the
    provider appears, leave them empty and it does not. The template reads
    `oauth_providers` from the context processor below rather than guessing.

    `server_metadata_url` is fetched lazily by authlib on first use, so this
    costs no network at start-up.
    """
    from app.core.auth.strategies.oauth_google import GoogleOAuthStrategy
    from app.core.auth.strategies.oauth_microsoft import MicrosoftOAuthStrategy

    # Each provider carries its own registrar rather than being looked up in a
    # dict of classes: a dict collapses both to `type[AuthStrategy]`, and
    # `register_client` lives on the concrete classes, not on the ABC.
    providers = (
        ("google", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", GoogleOAuthStrategy.register_client),
        (
            "microsoft",
            "MICROSOFT_CLIENT_ID",
            "MICROSOFT_CLIENT_SECRET",
            MicrosoftOAuthStrategy.register_client,
        ),
    )
    registered: list[str] = []

    for name, id_key, secret_key, register_client in providers:
        if not (app.config.get(id_key) and app.config.get(secret_key)):
            continue
        try:
            register_client(app)
        except Exception:  # noqa: BLE001 — one bad provider must not stop boot
            logger = structlog.get_logger()
            logger.warning("oauth_provider_registration_failed", provider=name)
            continue
        registered.append(name)

    app.config["OAUTH_PROVIDERS"] = registered

    @app.context_processor
    def _expose_oauth_providers():
        return {"oauth_providers": registered}

    return registered


def _register_security_headers(app: Flask) -> None:
    """Headers the app was serving none of.

    In the app rather than in nginx on purpose. The nginx config lives in
    docs/DEPLOYMENT.md, not in version control, so it is a thing each new
    server gets by being retyped correctly -- and the one that is retyped
    wrong is silent. These travel with the code.

    `setdefault`, so a proxy that already sets one wins: a deployment that
    does harden nginx should not end up with two conflicting policies.

    **Referrer-Policy is the one that matters most here.** This app links out
    to publishers from every paper card. Under the browser default the full
    referring URL goes with the click, and this app's URLs carry the user's
    own search: `/library/search?q=...`. `strict-origin-when-cross-origin`
    sends the origin alone off-site and keeps the path internally.

    **HSTS is conditional on `SESSION_COOKIE_SECURE`**, which is the same
    thing as "we are behind TLS". Sending it over plain HTTP in development
    would pin the browser to HTTPS for that host for a year, and `localhost`
    is a host shared with every other project on the machine -- the kind of
    breakage that outlives the session that caused it.

    **No `Content-Security-Policy` here.** Eight templates still carry inline
    `<script>`, so the only CSP that would not break the app today is one with
    `'unsafe-inline'`, which is not a policy. That work is tracked separately
    (PRELAUNCH Y4) and starts with moving those scripts out, not with a header.
    """

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # DENY rather than SAMEORIGIN: nothing in this app frames itself.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def _apply_proxy_fix(app: Flask) -> None:
    """Read X-Forwarded-* when a reverse proxy is declared in front.

    nginx already sends these (docs/DEPLOYMENT.md §3) and Flask ignored all of
    them, which broke two things quietly:

    * `request.remote_addr` was the proxy on every request, so the
      `10 per minute` limit on `/auth/login` was not per client -- it was one
      bucket for the entire site. Brute-force protection stopped existing, and
      a single attacker could lock everyone else out by exhausting it.
    * `request.is_secure` was False, so `url_for(..., _external=True)` built
      `http://` links. Password-reset emails carried them.

    Applied before `_init_extensions` because Flask-Limiter resolves the client
    address through the WSGI environment, and the middleware has to be wrapping
    it by the time the limiter is bound.

    Off unless `PROXY_FIX_HOPS` says otherwise -- see the config for why the
    number errs low.
    """
    hops = int(app.config.get("PROXY_FIX_HOPS", 0) or 0)
    if hops <= 0:
        return

    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
        app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops, x_port=0, x_prefix=0
    )


def _init_extensions(app: Flask) -> None:
    db.init_app(app)

    migrate.init_app(app, db)
    login_manager.init_app(app)
    oauth.init_app(app)
    _register_oauth_providers(app)
    csrf.init_app(app)
    limiter.init_app(app)

    mail.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    from app.core.i18n.utils import init_babel

    init_babel(app, babel)


def _init_observability(app: Flask) -> None:
    """Sentry + /metrics — both inert unless configured. See core/observability."""
    from app.core.observability import init_observability

    init_observability(app)


def _init_celery(app: Flask) -> None:
    """Bind the singleton Celery app to this Flask app (idempotent)."""
    from app.tasks import init_celery

    init_celery(app)


def _init_logging(app: Flask) -> None:
    # Dev / testing: human-readable console renderer.
    # Anywhere else (production, staging): JSON renderer for log aggregators.
    renderer = (
        structlog.dev.ConsoleRenderer()
        if app.debug or app.testing
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ]
    )


def _register_session_guard(app: Flask) -> None:
    """Her istekte session key geçerliliğini kontrol eder."""

    @app.before_request
    def _check_session():
        from flask_login import current_user, logout_user

        if not current_user.is_authenticated:
            return
        from app.core.sessions.service import get_current_key, touch_session

        key = get_current_key()
        if key is None:
            # Eski oturum (session tracking öncesi) — geçerli say, key üret
            return
        record = touch_session(key, expected_user_id=current_user.id)
        if record is None:
            # Oturum revoke edilmiş
            logout_user()
            from flask import redirect, url_for

            return redirect(url_for("auth.login"))


def _register_context_processors(app: Flask) -> None:
    from flask_login import current_user

    @app.context_processor
    def inject_menu() -> dict:
        """Inject menu_nodes and current_user_permissions into templates.

        Context processors run for *every* `render_template`, including ones
        with no request behind them -- a template-backed email sent from a
        Celery task is the case that matters here. Outside a request
        `current_user` is None rather than an anonymous user, so reaching for
        `.is_authenticated` raised AttributeError and took the render with it.

        There is no user to build a menu for in that situation, and an email
        has no sidebar, so the empty answer is the correct one rather than a
        fallback. Today only `send_password_reset` and `send_email_verification`
        render templates and both run inside a request; this is here so the
        first task that renders one does not rediscover it the way the saved
        searches did.
        """
        if has_request_context() and current_user.is_authenticated:
            from app.core.menu.builder import build_menu_for_user
            from app.core.rbac.service import get_user_permissions

            # We catch broadly here because this runs on every authenticated
            # request — if the menu/perm layer is broken, we'd rather degrade
            # to "no sidebar / no perms" than serve a 500. But always log the
            # traceback so the breakage is visible.
            try:
                nodes = build_menu_for_user(current_user)
            except Exception:
                logger.exception("menu_build_failed", user_id=current_user.id)
                nodes = []
            try:
                perms = get_user_permissions(current_user)
            except Exception:
                logger.exception("permission_load_failed", user_id=current_user.id)
                perms = frozenset()
            return {"menu_nodes": nodes, "current_user_permissions": perms}
        return {"menu_nodes": [], "current_user_permissions": frozenset()}

    @app.context_processor
    def inject_health_probe() -> dict:
        """Expose `system_health` as a callable, not a computed value.

        The sidebar status panel is admin-only, so this must not cost anything
        for the users who never see it — passing the function lets the template
        call it *inside* the permission guard. It runs at most once per render.
        """
        from app.core.health import system_health

        return {"system_health": system_health}

    # Note: there used to be an `inject_active_sources` context processor here
    # exposing the *deployment's* enabled sources. Its only consumer was the
    # scraper widget, which needs this user's effective set instead — a source
    # they muted must not be advertised as one that will be scanned. That now
    # comes from `scrape.service.scan_status_context` via `_widget_ctx()`.


def _register_blueprints(app: Flask) -> None:
    from app.api.v1 import api_v1_bp
    from app.core.audit.routes import audit_bp
    from app.core.auth import auth_bp
    from app.core.menu.routes import menu_bp
    from app.core.rbac.routes import rbac_bp
    from app.core.search.routes import search_bp
    from app.core.settings.routes import settings_bp
    from app.core.tasks_admin.routes import tasks_admin_bp
    from app.core.users.routes import users_bp
    from app.modules.academic import academic_bp
    from app.modules.dashboard import dashboard_bp
    from app.modules.scrape.alert_routes import alerts_bp
    from app.modules.scrape.library_routes import library_bp
    from app.modules.scrape.routes import scrape_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(rbac_bp, url_prefix="/admin/rbac")
    app.register_blueprint(menu_bp, url_prefix="/admin/menu")
    app.register_blueprint(users_bp, url_prefix="/admin/users")
    app.register_blueprint(tasks_admin_bp, url_prefix="/admin/tasks")
    app.register_blueprint(settings_bp, url_prefix="/settings")
    app.register_blueprint(audit_bp, url_prefix="/admin/audit")
    app.register_blueprint(scrape_bp, url_prefix="/papers")
    app.register_blueprint(library_bp, url_prefix="/library")
    app.register_blueprint(alerts_bp, url_prefix="/library")
    app.register_blueprint(academic_bp, url_prefix="/academic")
    app.register_blueprint(search_bp, url_prefix="/")
    app.register_blueprint(dashboard_bp, url_prefix="/")

    # JSON API — token auth, so exempt from the session-cookie CSRF guard.
    app.register_blueprint(api_v1_bp, url_prefix="/api/v1")
    csrf.exempt(api_v1_bp)


def _register_error_handlers(app: Flask) -> None:
    from flask import jsonify, render_template, request

    def _wants_json() -> bool:
        return request.path.startswith("/api/")

    @app.errorhandler(403)
    def forbidden(e):
        if _wants_json():
            return jsonify({"error": {"code": "forbidden", "message": "Forbidden."}}), 403
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        if _wants_json():
            return jsonify({"error": {"code": "not_found", "message": "Resource not found."}}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        if _wants_json():
            return (
                jsonify(
                    {"error": {"code": "method_not_allowed", "message": "Method not allowed."}}
                ),
                405,
            )
        return render_template("errors/404.html"), 405

    @app.errorhandler(500)
    def server_error(e):
        if _wants_json():
            return jsonify({"error": {"code": "server_error", "message": "Internal error."}}), 500
        return render_template("errors/500.html"), 500
