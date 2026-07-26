"""System health probe + the sidebar panel that renders it.

The probe deliberately avoids `celery inspect ping` (a broadcast RPC that
blocks for its whole timeout when nothing answers — i.e. slowest exactly when
things are broken) and reads the heartbeat stamp instead. These tests pin that
contract down, plus the two rules the panel must never break: it is admin-only,
and it can never take a page down with it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.health import (
    DOWN,
    HEARTBEAT_KEY,
    HEARTBEAT_STALE_AFTER,
    OK,
    UNKNOWN,
    record_heartbeat,
    system_health,
)


class _FakeRedis:
    """Just the three commands `health.py` uses."""

    def __init__(self, store=None, *, fail=False):
        self.store = dict(store or {})
        self.fail = fail
        self.queues = {}

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value

    def llen(self, key):
        return self.queues.get(key, 0)


def _use(monkeypatch, client):
    monkeypatch.setattr("app.core.health._client", lambda: client)


def _stamp(offset_seconds: int = 0, worker: str = "celery@host") -> str:
    when = datetime.now(UTC) - timedelta(seconds=offset_seconds)
    return f"{when.isoformat()}|{worker}"


def test_fresh_heartbeat_reads_as_a_live_worker(app, db, monkeypatch):
    _use(monkeypatch, _FakeRedis({HEARTBEAT_KEY: _stamp(5)}))
    with app.app_context():
        h = system_health()
        assert h["worker"] == OK
        assert h["redis"] == OK
        assert h["database"] == OK
        assert h["worker_name"] == "celery@host"
        assert h["all_ok"] is True


def test_stale_heartbeat_reads_as_a_dead_worker(app, db, monkeypatch):
    """A stamp older than the staleness window means Beat scheduled nothing or
    no worker consumed it — either way, scans will not run."""
    _use(monkeypatch, _FakeRedis({HEARTBEAT_KEY: _stamp(HEARTBEAT_STALE_AFTER + 60)}))
    with app.app_context():
        h = system_health()
        assert h["worker"] == DOWN
        assert h["all_ok"] is False


def test_missing_heartbeat_reads_as_down(app, db, monkeypatch):
    """The exact situation behind "my scan never finished": no worker ever ran,
    so nothing ever stamped the key."""
    _use(monkeypatch, _FakeRedis())
    with app.app_context():
        h = system_health()
        assert h["worker"] == DOWN
        assert h["worker_seen_at"] is None


def test_unreadable_heartbeat_is_unknown_not_down(app, db, monkeypatch):
    """"We can't tell" must not be reported as "it's broken"."""
    _use(monkeypatch, _FakeRedis(fail=True))
    with app.app_context():
        assert system_health()["worker"] == UNKNOWN


def test_unreachable_redis_degrades_without_raising(app, db, monkeypatch):
    _use(monkeypatch, None)
    with app.app_context():
        h = system_health()
        assert h["redis"] == DOWN
        assert h["worker"] == UNKNOWN
        assert h["queued"] is None
        assert h["all_ok"] is False


def test_queue_depth_is_summed_across_declared_queues(app, db, monkeypatch):
    client = _FakeRedis({HEARTBEAT_KEY: _stamp(1)})
    client.queues = {"celery": 1, "scrape": 4, "io": 0, "llm": 2}
    _use(monkeypatch, client)
    with app.app_context():
        h = system_health()
        assert h["queued"] == 7
        assert h["queues"]["scrape"] == 4


def test_heartbeat_task_stamps_the_key(app, db, monkeypatch):
    """The task is the only writer — a fresh stamp is what proves Beat AND a
    worker are both alive, which is the whole point of using it as the probe."""
    from app.tasks.core_tasks import heartbeat

    client = _FakeRedis()
    _use(monkeypatch, client)
    with app.app_context():
        heartbeat.delay().get()
        assert HEARTBEAT_KEY in client.store
        assert system_health()["worker"] == OK


def test_record_heartbeat_is_a_noop_without_redis(app, monkeypatch):
    _use(monkeypatch, None)
    with app.app_context():
        record_heartbeat("celery@host")  # must not raise


# ----------------------------------------------------------------------------
# The sidebar panel
# ----------------------------------------------------------------------------


def test_panel_is_hidden_from_regular_users(auth_client, monkeypatch):
    """Infra state is admin-only: a researcher's version of "the worker is
    down" is the honest queued-scan message, not a status board."""
    _use(monkeypatch, _FakeRedis({HEARTBEAT_KEY: _stamp(5)}))
    client, _uid = auth_client
    body = client.get("/").get_data(as_text=True)
    assert 'data-testid="system-health"' not in body


def test_panel_renders_for_an_admin(app, db, monkeypatch):
    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.user import User

    client_redis = _FakeRedis({HEARTBEAT_KEY: _stamp(5)})
    client_redis.queues = {"celery": 0, "scrape": 2, "io": 0, "llm": 0}
    _use(monkeypatch, client_redis)

    admin = User(
        username="healthadmin",
        email="healthadmin@example.test",
        full_name="Health Admin",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
        is_superuser=True,
    )
    db.session.add(admin)
    db.session.commit()

    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True

    body = c.get("/", follow_redirects=True).get_data(as_text=True)
    assert 'data-testid="system-health"' in body
    assert 'data-testid="health-worker"' in body
    assert 'data-testid="health-queued"' in body

    db.session.query(User).filter_by(id=admin.id).delete()
    db.session.commit()


def test_probe_is_total(app, db, monkeypatch):
    """A status widget that can 500 the app is worse than no status widget, so
    every failure path has to resolve to "unknown" instead of raising."""

    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("app.core.health._client", _boom)
    with app.app_context():
        h = system_health()
        assert h["worker"] == UNKNOWN
        assert h["all_ok"] is False


def test_panel_never_breaks_the_page(app, db, monkeypatch):
    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.user import User

    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("app.core.health._client", _boom)

    admin = User(
        username="healthadmin2",
        email="healthadmin2@example.test",
        full_name="Health Admin 2",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
        is_superuser=True,
    )
    db.session.add(admin)
    db.session.commit()

    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True

    r = c.get("/", follow_redirects=True)
    assert r.status_code == 200
    assert 'data-testid="system-health"' in r.get_data(as_text=True)

    db.session.query(User).filter_by(id=admin.id).delete()
    db.session.commit()
