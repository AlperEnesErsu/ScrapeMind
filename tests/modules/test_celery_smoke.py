"""Celery integration smoke — eager mode in tests, no real worker needed."""

from app.tasks import celery_app
from app.tasks.core_tasks import heartbeat, ping


def test_celery_eager_mode_enabled(app):
    assert app.config["CELERY_TASK_ALWAYS_EAGER"] is True
    assert celery_app.conf.task_always_eager is True


def test_ping_returns_pong(app):
    assert ping.delay().get() == "pong"


def test_heartbeat_payload_has_timestamp(app):
    payload = heartbeat.delay().get()
    assert "at" in payload
    assert "worker" in payload


def test_registered_tasks_visible():
    names = set(celery_app.tasks.keys())
    assert "core.ping" in names
    assert "core.heartbeat" in names


def test_beat_schedule_loaded():
    schedule = celery_app.conf.beat_schedule
    assert "core-heartbeat-every-minute" in schedule


def test_tasks_admin_route_requires_login(client):
    r = client.get("/admin/tasks/", follow_redirects=False)
    assert r.status_code in (302, 401)
