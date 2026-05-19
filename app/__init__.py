import structlog
from flask import Flask

from app.config import get_config
from app.extensions import babel, csrf, db, limiter, login_manager, mail, migrate, oauth

logger = structlog.get_logger()


def create_app() -> Flask:
    import app.core.models  # noqa: F401 — registers all SQLAlchemy models before init

    app = Flask(__name__, template_folder="core/templates", static_folder="core/static")
    app.config.from_object(get_config())

    _init_extensions(app)
    _init_logging(app)
    _init_celery(app)
    _register_blueprints(app)
    _register_session_guard(app)
    _register_context_processors(app)
    _register_error_handlers(app)

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


def _init_extensions(app: Flask) -> None:
    db.init_app(app)

    migrate.init_app(app, db)
    login_manager.init_app(app)
    oauth.init_app(app)
    babel.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    mail.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    from app.core.i18n.utils import init_babel

    init_babel(app, babel)


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
        record = touch_session(key)
        if record is None:
            # Oturum revoke edilmiş
            logout_user()
            from flask import redirect, url_for
            return redirect(url_for("auth.login"))


def _register_context_processors(app: Flask) -> None:
    from flask_login import current_user

    @app.context_processor
    def inject_menu() -> dict:
        """Inject menu_nodes and current_user_permissions into templates."""
        if current_user.is_authenticated:
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


def _register_blueprints(app: Flask) -> None:
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
    from app.modules.scrape.routes import scrape_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(rbac_bp, url_prefix="/admin/rbac")
    app.register_blueprint(menu_bp, url_prefix="/admin/menu")
    app.register_blueprint(users_bp, url_prefix="/admin/users")
    app.register_blueprint(tasks_admin_bp, url_prefix="/admin/tasks")
    app.register_blueprint(settings_bp, url_prefix="/settings")
    app.register_blueprint(audit_bp, url_prefix="/admin/audit")
    app.register_blueprint(scrape_bp, url_prefix="/papers")
    app.register_blueprint(academic_bp, url_prefix="/academic")
    app.register_blueprint(search_bp, url_prefix="/")
    app.register_blueprint(dashboard_bp, url_prefix="/")


def _register_error_handlers(app: Flask) -> None:
    from flask import render_template

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("errors/500.html"), 500
