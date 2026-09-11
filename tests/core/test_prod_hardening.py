"""Production hardening that is invisible until it is missing.

Both things here failed the same way: a setting that looks configured and is
not read. The rate-limit store was spelled `RATELIMIT_STORAGE_URL` while
Flask-Limiter reads `RATELIMIT_STORAGE_URI`, so a deployment could put Redis in
it and still get per-process counters. The remember-me cookie was never
configured at all, so it inherited Flask-Login's defaults rather than the
hardening the session cookie got.

Neither shows up in a page, a log line, or a test that does not look for it.
"""

from __future__ import annotations

import pytest


class _StubLimiter:
    """Stands in for Flask-Limiter's storage selection.

    Asserting on the real limiter's storage would need a live Redis; what
    matters here is only which *config key* the library is handed.
    """

    @staticmethod
    def storage_key_read_by_flask_limiter() -> str:
        from flask_limiter.constants import ConfigVars

        return ConfigVars.STORAGE_URI


def test_the_config_key_is_the_one_the_library_reads():
    """The bug in one line.

    Flask-Limiter renamed this key; the config kept the old spelling. Pinning
    it against the library's own constant means a future rename is a failing
    test rather than another silent downgrade to in-memory counters.
    """
    from app.config import BaseConfig

    key = _StubLimiter.storage_key_read_by_flask_limiter()

    assert key == "RATELIMIT_STORAGE_URI"
    assert hasattr(BaseConfig, key), f"config must define {key}, the key the library reads"


def test_the_old_env_var_name_still_works(monkeypatch):
    """An existing .env must not silently lose its setting in the rename."""
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    monkeypatch.setenv("RATELIMIT_STORAGE_URL", "redis://example.test:6379/1")

    import importlib

    from app import config as config_module

    importlib.reload(config_module)
    try:
        assert config_module.BaseConfig.RATELIMIT_STORAGE_URI == "redis://example.test:6379/1"
    finally:
        importlib.reload(config_module)


def test_production_refuses_to_boot_on_in_memory_rate_limiting(monkeypatch):
    """In-memory counters are per process, and production runs four of them.

    A refusal rather than a warning, because the endpoint this protects is the
    login form and a start-up warning is read after the incident, not before.
    """
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    monkeypatch.delenv("RATELIMIT_STORAGE_URL", raising=False)

    import importlib

    from app import config as config_module

    importlib.reload(config_module)
    try:
        from app import create_app

        with pytest.raises(RuntimeError, match="RATELIMIT_STORAGE_URI"):
            create_app()
    finally:
        importlib.reload(config_module)


def test_the_remember_cookie_is_hardened_like_the_session_cookie(app):
    """Flask-Login does not inherit SESSION_COOKIE_*; it has its own keys.

    This cookie outlives the session cookie, so leaving it the weaker of the
    two undoes the point of hardening the session cookie.
    """
    assert app.config["REMEMBER_COOKIE_HTTPONLY"] is True
    assert app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"


def test_production_marks_both_cookies_secure():
    """Secure lives on ProductionConfig for both, so they cannot drift apart."""
    from app.config import ProductionConfig

    assert ProductionConfig.SESSION_COOKIE_SECURE is True
    assert ProductionConfig.REMEMBER_COOKIE_SECURE is True
