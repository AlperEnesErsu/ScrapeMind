"""The admin overview's health panel.

It used to run `celery inspect ping` on every render -- a broadcast RPC that
blocks for its whole timeout when nothing answers, so this page took twelve
seconds exactly when an admin had opened it to find out what was broken. It now
reads the same two Redis keys the sidebar does, which also stops the two panels
contradicting each other on one screen.

The thread-locality bug these tests were written for is gone with the ping: the
probe no longer touches `celery.current_app` at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.health import HEARTBEAT_KEY, WORKER_KEY
from app.modules.dashboard import routes as dashboard_routes


class _FakeRedis:
    """Just the commands `app/core/health.py` uses."""

    def __init__(self, store=None):
        self.store = dict(store or {})

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value

    def llen(self, key):
        return 0


def _stamp(offset_seconds: int = 0, name: str = "celery@host") -> str:
    when = datetime.now(UTC) - timedelta(seconds=offset_seconds)
    return f"{when.isoformat()}|{name}"


def _use(monkeypatch, client):
    monkeypatch.setattr("app.core.health._client", lambda: client)


def test_a_live_worker_and_scheduler_both_read_up(app, monkeypatch):
    _use(monkeypatch, _FakeRedis({WORKER_KEY: _stamp(5), HEARTBEAT_KEY: _stamp(5)}))
    with app.app_context():
        status = dashboard_routes._health_status()

    assert status == {"db": "ok", "redis": "ok", "worker": "ok", "beat": "ok"}


def test_a_stopped_scheduler_does_not_read_as_a_dead_worker(app, monkeypatch):
    """The same separation the sidebar makes -- and now from the same keys, so
    the two panels cannot disagree."""
    _use(monkeypatch, _FakeRedis({WORKER_KEY: _stamp(5), HEARTBEAT_KEY: _stamp(9999)}))
    with app.app_context():
        status = dashboard_routes._health_status()

    assert status["worker"] == "ok"
    assert status["beat"] == "down"


def test_an_unreachable_broker_is_reported_not_raised(app, monkeypatch):
    """A status panel must not 500 the page it is a corner of."""
    _use(monkeypatch, None)
    with app.app_context():
        status = dashboard_routes._health_status()

    assert status["redis"] == "down"
    assert status["worker"] == "unknown"


def test_the_probe_does_not_broadcast(app, monkeypatch):
    """The point of the rewrite. `inspect().ping()` is what made this page take
    twelve seconds with the broker down; nothing here may call it."""
    from app.tasks import celery_app

    def _forbidden(*a, **kw):
        raise AssertionError("the health panel must not broadcast an inspect ping")

    monkeypatch.setattr(celery_app.control, "inspect", _forbidden)
    monkeypatch.setattr(celery_app, "connection_for_write", _forbidden)
    _use(monkeypatch, _FakeRedis({WORKER_KEY: _stamp(5), HEARTBEAT_KEY: _stamp(5)}))

    with app.app_context():
        assert dashboard_routes._health_status()["worker"] == "ok"
