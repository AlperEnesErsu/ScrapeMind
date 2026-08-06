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
    # MAIL_SERVER bos kalirsa: dev modu — gercek SMTP cagrisi yapilmaz, link `flash` ile gosterilir.
    # MAIL_USE_TLS (587/STARTTLS) ve MAIL_USE_SSL (465) birbirini disar. Ayni anda ikisini true verme.
    MAIL_SERVER = os.getenv("MAIL_SERVER", "")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USE_SSL = os.getenv("MAIL_USE_SSL", "false").lower() == "true"
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "noreply@example.com")
    # Explicit override beats inference: useful for staging where MAIL_SERVER is set
    # but you want to *temporarily* suppress real sends without unsetting credentials.
    _mail_suppress_env = os.getenv("MAIL_SUPPRESS_SEND")
    MAIL_SUPPRESS_SEND = (
        _mail_suppress_env.lower() == "true"
        if _mail_suppress_env is not None
        else not bool(MAIL_SERVER)
    )

    # Celery (Phase 2). Broker + result backend share the same Redis instance.
    _REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", _REDIS_URL)
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", _REDIS_URL)
    CELERY_TASK_ALWAYS_EAGER = False  # overridden in TestingConfig
    # Global task time limits. Without these a single hung network call pins a
    # worker slot forever; soft raises SoftTimeLimitExceeded (catchable, lets a
    # task commit what it has), hard SIGKILLs the child. Per-task decorators
    # override these where a tighter budget makes sense (e.g. feed fetching).
    CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "600"))
    CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "900"))

    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

    MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
    MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
    MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")

    # Anthropic Claude API — powers paper AI analysis + translation.
    # When empty, /papers/<id>?mode=ai shows a "configure to enable" panel
    # instead of crashing. Set in production to switch the feature on.
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    # LLM provider abstraction (Faz 1 digest) — "openrouter" | "ollama" | "anthropic".
    # Default is openrouter because it has genuinely free (":free" suffixed) models,
    # so a fresh deployment can turn AI features on at zero cost. Per-user keys
    # (see UserSettings.settings["llm"]) take priority over the global fallback
    # below; Ollama needs no key at all (local inference).
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openrouter")

    # OpenRouter — https://openrouter.ai/keys. OPENROUTER_API_KEY is an optional
    # *global* fallback used when a user hasn't set their own key yet.
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct:free")
    OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    # Ollama — local inference, no API key required.
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5")

    # Fernet key encrypting per-user LLM API keys at rest (UserSettings JSON).
    # If empty, derived from SECRET_KEY (sha256 -> urlsafe base64) so dev/test
    # never need a separate secret — production should still set one explicitly
    # so rotating SECRET_KEY doesn't strand every stored key.
    LLM_ENC_KEY = os.getenv("LLM_ENC_KEY", "")

    # Audit log retention — rows older than this many days are purged by the
    # nightly `core.purge_audit_logs` task. 0 disables purging (keep forever).
    AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "180"))

    # ---- Scanning: history, fan-out, feed fetching -------------------------
    # ScanRun rows older than this are purged by `scrape.purge_scan_runs`.
    # 0 disables purging (keep forever), same contract as AUDIT_RETENTION_DAYS.
    SCAN_RUN_RETENTION_DAYS = int(os.getenv("SCAN_RUN_RETENTION_DAYS", "30"))
    # Nightly fan-out spreads users deterministically over this many seconds
    # instead of enqueueing every user at the same instant. Deterministic (not
    # random) so the "next scan at 03:27" we show a user is actually true.
    SCAN_FANOUT_WINDOW_SECONDS = int(os.getenv("SCAN_FANOUT_WINDOW_SECONDS", "1800"))
    # Custom RSS feeds a single user may register. Each active feed costs one
    # HTTP fetch per nightly run, so this is the main per-user cost knob.
    MAX_USER_FEEDS = int(os.getenv("MAX_USER_FEEDS", "50"))
    # YouTube channels a single user may subscribe to. This is only the
    # fallback used when no SystemSettings row exists yet — the effective
    # value is the admin-editable DB setting (see settings/system route).
    # Default is lower than MAX_USER_FEEDS because each channel costs a
    # nightly feed fetch plus, potentially, a transcript download and an LLM
    # summary.
    MAX_USER_CHANNELS = int(os.getenv("MAX_USER_CHANNELS", "10"))
    # Read timeout (seconds) and hard body cap (bytes) for a feed fetch.
    FEED_FETCH_TIMEOUT = int(os.getenv("FEED_FETCH_TIMEOUT", "15"))
    FEED_FETCH_MAX_BYTES = int(os.getenv("FEED_FETCH_MAX_BYTES", "5242880"))  # 5 MiB
    # SSRF guard escape hatch — only for local development against a feed
    # served from localhost. Never enable in production.
    FEED_ALLOW_PRIVATE_HOSTS = os.getenv("FEED_ALLOW_PRIVATE_HOSTS", "false").lower() == "true"
    # Shared external API budgets. These are per-deployment, enforced through
    # Redis, because the per-user architecture otherwise multiplies each
    # adapter's own client-side throttle by the number of concurrent workers.
    SCRAPE_RATE_ARXIV_PER_MIN = int(os.getenv("SCRAPE_RATE_ARXIV_PER_MIN", "20"))
    SCRAPE_RATE_S2_PER_5MIN = int(os.getenv("SCRAPE_RATE_S2_PER_5MIN", "100"))
    SCRAPE_RATE_PUBMED_PER_SEC = int(os.getenv("SCRAPE_RATE_PUBMED_PER_SEC", "3"))
    SCRAPE_RATE_OPENALEX_PER_SEC = int(os.getenv("SCRAPE_RATE_OPENALEX_PER_SEC", "8"))
    SCRAPE_RATE_CROSSREF_PER_SEC = int(os.getenv("SCRAPE_RATE_CROSSREF_PER_SEC", "5"))
    # These three back agent_reach_source's youtube/github/web adapters — added
    # here so they're actually tunable; previously they only ever hit the
    # `_cfg` fallback in ratelimit.py because no BaseConfig/.env.example entry
    # defined them.
    SCRAPE_RATE_WEB_PER_MIN = int(os.getenv("SCRAPE_RATE_WEB_PER_MIN", "30"))
    SCRAPE_RATE_YOUTUBE_PER_MIN = int(os.getenv("SCRAPE_RATE_YOUTUBE_PER_MIN", "30"))
    SCRAPE_RATE_GITHUB_PER_MIN = int(os.getenv("SCRAPE_RATE_GITHUB_PER_MIN", "30"))
    # yt-dlp transcript fetches (youtube_channel_source.fetch_transcript) —
    # a separate bucket from SCRAPE_RATE_YOUTUBE_PER_MIN (agent_reach_source's
    # video *search*): one video summary per new upload, not a search call.
    SCRAPE_RATE_YT_CHANNEL_PER_MIN = int(os.getenv("SCRAPE_RATE_YT_CHANNEL_PER_MIN", "30"))

    # Redis cache for RBAC permission sets (see app/core/cache.py). Purely an
    # optimisation: with CACHE_ENABLED=false, or Redis unreachable, everything
    # reads through to Postgres.
    REDIS_URL = _REDIS_URL
    CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
    CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))  # seconds

    # Observability (see app/core/observability.py). Both fully optional:
    # empty SENTRY_DSN = no Sentry; METRICS_ENABLED=false = no /metrics.
    SENTRY_DSN = os.getenv("SENTRY_DSN", "")
    SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0"))
    METRICS_ENABLED = os.getenv("METRICS_ENABLED", "false").lower() == "true"

    # API v1 — JWT bearer auth for the JSON API under /api/v1.
    # JWT_SECRET_KEY is resolved at runtime with a fallback to SECRET_KEY (see
    # app/api/v1/tokens.py), so leaving it empty still works out of the box;
    # set it to rotate API tokens independently of the session cookie key.
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
    JWT_ALGORITHM = "HS256"
    JWT_ISSUER = os.getenv("JWT_ISSUER", "scrapemind")
    JWT_ACCESS_TTL = int(os.getenv("JWT_ACCESS_TTL", "900"))  # access token: 15 min
    JWT_REFRESH_TTL = int(os.getenv("JWT_REFRESH_TTL", "2592000"))  # refresh token: 30 days


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
    # Disable rate limiting in tests — otherwise repeated hits to limited
    # endpoints (e.g. /api/v1/auth/token) accumulate across tests and 429.
    RATELIMIT_ENABLED = False
    # CI has no Redis service, and a cache would make permission assertions
    # depend on eviction timing. Cache behaviour is tested explicitly with a
    # fake client instead (tests/core/test_cache.py).
    CACHE_ENABLED = False
    # CI has no outbound network, so a DNS-resolving SSRF guard would reject
    # every fixture URL. The guard is tested directly instead (it takes the
    # flag as an explicit argument), and the tests that assert rejection flip
    # this back to False with literal-IP URLs that need no resolution.
    FEED_ALLOW_PRIVATE_HOSTS = True
    # Point the lock/rate-limit Redis at a closed port so `_lock_client()`
    # fails open, same reasoning as CACHE_ENABLED=False above. A live Redis
    # here makes tests order-dependent: the test DB is recreated each session
    # so user ids restart at 1, but a 900s per-user lock from the previous run
    # is still in Redis and would silently skip the next run's scan. Lock
    # behaviour is asserted directly by stubbing acquire_scrape_lock instead.
    REDIS_URL = "redis://127.0.0.1:1/0"
    CELERY_BROKER_URL = "redis://127.0.0.1:1/0"
    CELERY_RESULT_BACKEND = "redis://127.0.0.1:1/0"


_config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def get_config() -> type[BaseConfig]:
    env = os.getenv("FLASK_ENV", "development")
    return _config_map.get(env, DevelopmentConfig)
