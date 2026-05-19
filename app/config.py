import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env dosyasini en erken noktada yukle — class body'deki os.environ[] cagrilari oncesinde
load_dotenv(BASE_DIR / ".env", override=False)


class BaseConfig:
    # Lazy fallback so importing this module (e.g. during alembic env.py or
    # CI's lint phase) doesn't crash before TestingConfig has a chance to
    # override. The fallback is intentionally insecure-looking so any
    # production deployment without SECRET_KEY in env stands out in logs.
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    BABEL_DEFAULT_LOCALE = os.getenv("BABEL_DEFAULT_LOCALE", "tr")
    BABEL_DEFAULT_TIMEZONE = os.getenv("BABEL_DEFAULT_TIMEZONE", "Europe/Istanbul")
    BABEL_TRANSLATION_DIRECTORIES = str(BASE_DIR / "translations")
    SUPPORTED_LOCALES = ["tr", "en"]

    WTF_CSRF_ENABLED = True

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    RATELIMIT_STORAGE_URL = os.getenv("RATELIMIT_STORAGE_URL", "memory://")

    # Email (Flask-Mail)
    MAIL_SERVER = os.getenv("MAIL_SERVER", "")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "noreply@example.com")
    MAIL_SUPPRESS_SEND = not bool(os.getenv("MAIL_SERVER", ""))  # MAIL_SERVER yoksa gönderme

    # Celery (Phase 2). Broker + result backend share the same Redis instance.
    _REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", _REDIS_URL)
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", _REDIS_URL)
    CELERY_TASK_ALWAYS_EAGER = False  # overridden in TestingConfig

    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

    MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
    MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
    MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL", "postgresql://scrapemind:scrapemind@localhost:5432/scrapemind"
    )
    SESSION_COOKIE_SECURE = False


class ProductionConfig(BaseConfig):
    DEBUG = False
    # Lazy-read so that just importing the module (e.g. during CI's lint/test
    # phases where FLASK_ENV=testing and only TEST_DATABASE_URL is set) doesn't
    # crash with KeyError. Production deployments must export DATABASE_URL —
    # SQLAlchemy will surface a clear connection error if it's empty.
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "")
    SESSION_COOKIE_SECURE = True


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "TEST_DATABASE_URL", "postgresql://scrapemind:scrapemind@localhost:5432/scrapemind_test"
    )
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret-key"  # noqa: S105
    # Run Celery tasks inline so tests don't need a worker process.
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True


_config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def get_config() -> type[BaseConfig]:
    env = os.getenv("FLASK_ENV", "development")
    return _config_map.get(env, DevelopmentConfig)
