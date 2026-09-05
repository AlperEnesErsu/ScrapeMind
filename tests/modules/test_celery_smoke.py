"""Celery integration smoke — eager mode in tests, no real worker needed."""

from app.tasks import INTENTIONALLY_UNROUTED_TASKS, TASK_ROUTES, celery_app
from app.tasks.core_tasks import heartbeat, ping
from app.tasks.schedule import BEAT_SCHEDULE


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


def test_every_beat_task_is_routed_or_intentionally_unrouted():
    """Regression guard for the "fan-out parent silently lands on the default
    queue" bug class (see app/tasks/__init__.py TASK_ROUTES comment): a task
    Beat actually schedules must either have an explicit route, or be listed
    in INTENTIONALLY_UNROUTED_TASKS with a stated reason. Anything else means
    a worker started with a specialized -Q list will never consume it."""
    for entry_name, entry in BEAT_SCHEDULE.items():
        task_name = entry["task"]
        assert task_name in TASK_ROUTES or task_name in INTENTIONALLY_UNROUTED_TASKS, (
            f"'{task_name}' (beat entry '{entry_name}') has no TASK_ROUTES entry and is not "
            "in INTENTIONALLY_UNROUTED_TASKS — it would silently land on the default 'celery' "
            "queue, which a specialized worker's -Q list may not consume."
        )
