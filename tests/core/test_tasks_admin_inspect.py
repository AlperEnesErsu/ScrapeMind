"""/admin/tasks asks Celery once when nobody is listening, and stops waiting
as soon as every worker that is listening has replied."""

from __future__ import annotations

import pytest

from app.core.tasks_admin import routes


class _FakeInspect:
    def __init__(self, calls, replies, **kwargs):
        self._calls = calls
        self._replies = replies
        self.kwargs = kwargs

    def __getattr__(self, method):
        def call():
            self._calls.append((method, self.kwargs))
            return self._replies.get(method)

        return call


@pytest.fixture
def fake_control(monkeypatch):
    state = {"calls": [], "replies": {}}

    def inspect(**kwargs):
        return _FakeInspect(state["calls"], state["replies"], **kwargs)

    monkeypatch.setattr(routes.celery_app.control, "inspect", inspect)
    return state


def test_no_worker_costs_one_broadcast_not_five(fake_control):
    panels = routes._inspect_panels()
    assert [m for m, _ in fake_control["calls"]] == ["ping"]
    assert panels == {p: {} for p in routes._PANELS}


def test_with_workers_each_call_is_addressed_and_limited(fake_control):
    fake_control["replies"] = {
        "ping": {"w1@host": {"ok": "pong"}, "w2@host": {"ok": "pong"}},
        "active": {"w1@host": [], "w2@host": []},
        "stats": {"w1@host": {"pid": 1}},
    }
    panels = routes._inspect_panels()

    methods = [m for m, _ in fake_control["calls"]]
    assert methods == ["ping", *routes._PANELS]
    for _method, kwargs in fake_control["calls"][1:]:
        # Without `limit`, a broadcast waits out its whole timeout even after
        # every worker has answered.
        assert kwargs["destination"] == ["w1@host", "w2@host"]
        assert kwargs["limit"] == 2
    assert panels["active"] == {"w1@host": [], "w2@host": []}
    assert panels["reserved"] == {}


def test_broker_down_degrades_to_empty_panels(monkeypatch):
    class _Raising:
        def __getattr__(self, method):
            def call():
                raise ConnectionError("broker unreachable")

            return call

    monkeypatch.setattr(routes.celery_app.control, "inspect", lambda **k: _Raising())
    assert routes._inspect_panels() == {p: {} for p in routes._PANELS}


def test_overview_page_renders_with_no_worker(auth_client, db, fake_control):
    from app.core.models.user import User

    client, uid = auth_client
    user = db.session.get(User, uid)
    user.is_superuser = True
    db.session.commit()

    r = client.get("/admin/tasks/")
    assert r.status_code == 200
    assert [m for m, _ in fake_control["calls"]] == ["ping"]
