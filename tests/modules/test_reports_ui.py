"""Reports UI (Faz 6) — the report archive, creation form, detail view, the
HTMX status-poll partial, and deletion.

The generation pipeline itself (`report_service.create_report`/`run_report`,
`app/tasks/report_tasks.generate`) is someone else's file and already
tested elsewhere (tests/modules/test_reports.py, tests/tasks/test_report_tasks.py)
— every test here either stubs `generate.delay` (creation only ever needs to
prove the task was *queued*, not that it ran) or writes a `Report` row
directly at whatever status/sections/stats it wants to assert against,
mirroring how test_author_ui.py drives UserAuthor rows straight through the
ORM for the same reason.
"""

from __future__ import annotations

from werkzeug.security import generate_password_hash

import app.tasks.report_tasks as report_tasks_module
from app.core.models.user import User
from app.modules.scrape.models import Report

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _make_report(user_id: int, **overrides) -> Report:
    from app.extensions import db

    fields = {
        "user_id": user_id,
        "kind": "topic",
        "title": "Test report (2022-2026)",
        "params": {"keywords": ["testing"], "years": 5},
        "status": "pending",
        "sections": None,
        "stats": None,
        "item_count": 0,
    }
    fields.update(overrides)
    row = Report(**fields)
    db.session.add(row)
    db.session.commit()
    return row


def _stub_delay(monkeypatch):
    """Replace `generate.delay` with a recorder instead of actually queuing
    a Celery task — no worker runs in this test suite."""
    calls = []

    class _FakeAsyncResult:
        id = "fake-task-id"

    def _fake_delay(report_id):
        calls.append(report_id)
        return _FakeAsyncResult()

    monkeypatch.setattr(report_tasks_module.generate, "delay", _fake_delay)
    return calls


# ----------------------------------------------------------------------------
# /reports — archive + creation
# ----------------------------------------------------------------------------


def test_reports_page_requires_login(client):
    resp = client.get("/papers/reports")
    assert resp.status_code in (301, 302)


def test_reports_page_renders(auth_client):
    client, _uid = auth_client
    resp = client.get("/papers/reports")
    assert resp.status_code == 200
    # Assert on the form's own markup rather than its label text: the page
    # renders in BABEL_DEFAULT_LOCALE ("tr" here), so an English label only
    # matches until someone translates it — which is not what this test is
    # about.
    body = resp.get_data(as_text=True)
    assert 'name="keywords"' in body
    assert 'action="/papers/reports/new"' in body or "/papers/reports/new" in body


def test_create_report_writes_a_row_and_queues_the_task(auth_client, db, monkeypatch):
    client, uid = auth_client
    calls = _stub_delay(monkeypatch)

    resp = client.post(
        "/papers/reports/new",
        data={"keywords": "machine learning, retrieval", "years": "5"},
    )
    assert resp.status_code in (301, 302)

    rows = Report.query.filter_by(user_id=uid).all()
    assert len(rows) == 1
    assert rows[0].kind == "topic"
    assert rows[0].status == "pending"
    assert rows[0].params["keywords"] == ["machine learning", "retrieval"]
    assert calls == [rows[0].id]


def test_create_report_htmx_returns_202_with_redirect(auth_client, db, monkeypatch):
    client, uid = auth_client
    _stub_delay(monkeypatch)

    resp = client.post(
        "/papers/reports/new",
        data={"keywords": "quantum computing", "years": "3"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 202
    row = Report.query.filter_by(user_id=uid).one()
    assert resp.headers.get("HX-Redirect") == f"/papers/reports/{row.id}"


def test_create_report_rejects_empty_keywords(auth_client, db, monkeypatch):
    client, uid = auth_client
    calls = _stub_delay(monkeypatch)

    resp = client.post("/papers/reports/new", data={"keywords": "", "years": "5"})
    assert resp.status_code in (301, 302)
    assert Report.query.filter_by(user_id=uid).count() == 0
    assert calls == []

    # The error surfaces as a flash message on the page it redirects back to.
    follow = client.get(resp.headers["Location"])
    body = follow.get_data(as_text=True)
    assert "correct the errors" in body or "hata" in body.lower()


# ----------------------------------------------------------------------------
# Ownership — someone else's report is a 404, not a 403
# ----------------------------------------------------------------------------


def _make_other_user(db):
    user = User.query.filter_by(email="otherreportui@example.test").first()
    if user is None:
        user = User(
            username="otherreportui",
            email="otherreportui@example.test",
            full_name="Other Report User",
            password_hash=generate_password_hash("x12345678"),
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
    return user


def test_detail_of_another_users_report_is_404(auth_client, db):
    client, _uid = auth_client
    other = _make_other_user(db)
    foreign = _make_report(other.id)

    try:
        assert client.get(f"/papers/reports/{foreign.id}").status_code == 404
    finally:
        db.session.delete(foreign)
        db.session.commit()


def test_status_of_another_users_report_is_404(auth_client, db):
    client, _uid = auth_client
    other = _make_other_user(db)
    foreign = _make_report(other.id, status="running")

    try:
        assert client.get(f"/papers/reports/{foreign.id}/status").status_code == 404
    finally:
        db.session.delete(foreign)
        db.session.commit()


def test_deleting_another_users_report_is_404(auth_client, db):
    client, _uid = auth_client
    other = _make_other_user(db)
    foreign = _make_report(other.id)

    try:
        resp = client.post(f"/papers/reports/{foreign.id}/delete")
        assert resp.status_code == 404
        assert Report.query.get(foreign.id) is not None
    finally:
        db.session.delete(foreign)
        db.session.commit()


# ----------------------------------------------------------------------------
# Status poll partial — self-stopping
# ----------------------------------------------------------------------------


def test_poll_partial_keeps_polling_while_running(auth_client, db):
    client, uid = auth_client
    row = _make_report(uid, status="running")

    resp = client.get(f"/papers/reports/{row.id}/status")
    assert resp.status_code == 200
    assert b"hx-trigger" in resp.data
    assert "HX-Refresh" not in resp.headers


def test_poll_partial_stops_once_finished(auth_client, db):
    client, uid = auth_client
    row = _make_report(
        uid,
        status="ok",
        sections={"tldr": "done"},
        stats={"total": 1, "since_year": 2020, "until_year": 2024},
        item_count=1,
    )

    resp = client.get(f"/papers/reports/{row.id}/status")
    assert resp.status_code == 200
    assert b"hx-trigger" not in resp.data
    assert resp.headers.get("HX-Refresh") == "true"


# ----------------------------------------------------------------------------
# Detail view — stats-only fallback when the AI summary is missing
# ----------------------------------------------------------------------------


def test_detail_shows_numeric_summary_when_sections_is_empty(auth_client, db):
    client, uid = auth_client
    row = _make_report(
        uid,
        status="partial",
        sections={},
        stats={
            "total": 42,
            "since_year": 2021,
            "until_year": 2026,
            "year_series": [{"year": 2021, "count": 42}],
            "top_authors": [],
            "top_venues": [],
            "top_topics": [],
            "oa_share": None,
            "growing_topics": [],
            "journal_quartiles": {},
        },
        item_count=42,
    )

    resp = client.get(f"/papers/reports/{row.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "42" in body
    # The "AI summary unavailable, numbers still stand" notice is locale-bound
    # (the page renders in BABEL_DEFAULT_LOCALE), so pin the locale rather
    # than asserting on whichever language the test config happens to default
    # to. What matters is that the notice is shown at all — a partial report
    # must never look like a complete one.
    # Assert on a stable marker, not on the notice's wording: the page renders
    # in BABEL_DEFAULT_LOCALE, so any language-specific substring only holds
    # until that string is translated. What must hold is that a partial report
    # never looks like a complete one.
    assert 'data-testid="report-partial-notice"' in body


def test_detail_shows_error_status_without_a_traceback(auth_client, db):
    client, uid = auth_client
    row = _make_report(uid, status="error", error="TimeoutError")

    resp = client.get(f"/papers/reports/{row.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "TimeoutError" in body
    assert "Traceback" not in body


# ----------------------------------------------------------------------------
# Delete
# ----------------------------------------------------------------------------


def test_delete_removes_own_report(auth_client, db):
    client, uid = auth_client
    row = _make_report(uid)
    report_id = row.id

    resp = client.post(f"/papers/reports/{report_id}/delete")
    assert resp.status_code in (301, 302)
    assert Report.query.get(report_id) is None


def test_delete_htmx_redirects_to_the_list(auth_client, db):
    client, uid = auth_client
    row = _make_report(uid)

    resp = client.post(f"/papers/reports/{row.id}/delete", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/papers/reports"
    assert Report.query.get(row.id) is None
