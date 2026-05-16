import structlog
from flask import Flask

from app.config import get_config
from app.extensions import babel, csrf, db, limiter, login_manager, migrate, oauth

logger = structlog.get_logger()


def create_app() -> Flask:
    app = Flask(__name__, template_folder="core/templates", static_folder="core/static")
    app.config.from_object(get_config())

    _init_extensions(app)
    _init_logging()
    _register_blueprints(app)
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
            logger.warning("plugin_discovery_skipped", reason="modules table not found — run flask db upgrade first")

    return app


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    oauth.init_app(app)
    babel.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    from app.core.i18n.utils import init_babel
    init_babel(app, babel)


def _init_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if True else structlog.processors.JSONRenderer(),
        ]
    )


def _register_blueprints(app: Flask) -> None:
    from app.core.auth import auth_bp
    from app.core.audit.routes import audit_bp
    from app.core.menu.routes import menu_bp
    from app.core.rbac.routes import rbac_bp
    from app.core.settings.routes import settings_bp
    from app.modules.dashboard import dashboard_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(rbac_bp, url_prefix="/admin/rbac")
    app.register_blueprint(menu_bp, url_prefix="/admin/menu")
    app.register_blueprint(settings_bp, url_prefix="/settings")
    app.register_blueprint(audit_bp, url_prefix="/admin/audit")
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
