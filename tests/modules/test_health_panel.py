"""The admin overview's health panel.

One test here carries the weight: the panel must probe *our* Celery app, in
whatever thread the request happens to land in. It did not, and so it reported
Redis disconnected and Celery offline unconditionally -- under every threaded
WSGI server, which is all of them.

The bug survived because a health panel saying something is down looks like
news about the system rather than news about the panel.
"""

from __future__ import annotations

import threading

from app.modules.dashboard import routes as dashboard_routes


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self):
        return None


def _probe_from_a_worker_thread() -> dict[str, object]:
    """Run the health probe off the main thread, as a request thread does."""
    out: dict[str, dict[str, object]] = {}
    thread = threading.Thread(target=lambda: out.setdefault("r", dashboard_routes._health_status()))
    thread.start()
    thread.join()
    return out["r"]


def test_the_panel_probes_our_broker_from_a_request_thread(app, monkeypatch):
    """The regression.

    `celery.current_app` is thread-local: off the creating thread it is a bare
    `Celery('default')` with no broker. Patching *our* app's method and then
    probing from another thread is what tells the two apart -- with the old
    code the patch is never reached, the default app dials amqp, and the
    answer is "disconnected" no matter what.
    """
    from app.tasks import celery_app

    reached = []
    monkeypatch.setattr(
        celery_app, "connection_for_write", lambda *a, **kw: reached.append(1) or _Conn()
    )

    with app.app_context():
        status = _probe_from_a_worker_thread()

    assert reached, "the probe must go through our configured Celery app, not celery.current_app"
    assert status["redis"] == "connected"


def test_an_unreachable_broker_is_reported_not_raised(app, monkeypatch):
    """A status panel must not 500 the page it is a corner of."""
    from app.tasks import celery_app

    def _boom(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(celery_app, "connection_for_write", _boom)

    with app.app_context():
        status = _probe_from_a_worker_thread()

    assert status["redis"] == "disconnected"
    assert status["db"] == "connected"


def test_no_workers_is_told_apart_from_no_broker(app, monkeypatch):
    """ "Redis is up but nothing is consuming the queue" is a different
    problem from "Redis is down", and the panel has to say which."""
    from app.tasks import celery_app

    monkeypatch.setattr(celery_app, "connection_for_write", lambda *a, **kw: _Conn())

    class _Inspect:
        def ping(self):
            return None

    monkeypatch.setattr(celery_app.control, "inspect", lambda *a, **kw: _Inspect())

    with app.app_context():
        status = _probe_from_a_worker_thread()

    assert status["redis"] == "connected"
    assert status["celery"] == "idle"
    assert status["workers"] == 0


def test_workers_are_counted_not_described(app, monkeypatch):
    """The template used to pick the badge colour with `'active' in status`.

    Matching an English word to decide a colour meant the panel could not be
    translated without turning every badge red, so the count comes back as a
    number and the state as a code.
    """
    from app.tasks import celery_app

    monkeypatch.setattr(celery_app, "connection_for_write", lambda *a, **kw: _Conn())

    class _Inspect:
        def ping(self):
            return {"w1": {"ok": "pong"}, "w2": {"ok": "pong"}}

    monkeypatch.setattr(celery_app.control, "inspect", lambda *a, **kw: _Inspect())

    with app.app_context():
        status = _probe_from_a_worker_thread()

    assert status["celery"] == "active"
    assert status["workers"] == 2
