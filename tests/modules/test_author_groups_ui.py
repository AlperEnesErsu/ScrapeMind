"""Author search + author-group management UI (Faz 6, ADR-0003).

Covers the routes in `routes.py`'s "Author name search" and "Author groups"
sections: `submit_author_search`, `submit_author_add_to_group`,
`submit_group_create/rename/delete`, `submit_group_member_add/remove`, and
`submit_group_report`. The backend (`service.follow_author`/
`create_author_group`/`add_group_member`/... and
`openalex_source.search_authors`) is someone else's file and already tested
in tests/modules/test_author_groups.py — every test here drives it through
HTTP and only checks the observable result (DB row, flash text, redirect),
mirroring tests/modules/test_author_ui.py and tests/modules/test_reports_ui.py.

No real network calls: `oa._session.get` is monkeypatched throughout, and
`report_tasks.generate.delay` is stubbed so no Celery worker is needed.
"""

from __future__ import annotations

import pytest
import requests
from sqlalchemy import text
from werkzeug.security import generate_password_hash

import app.tasks.report_tasks as report_tasks_module
from app.core.models.user import User
from app.modules.scrape import service
from app.modules.scrape.models import AuthorGroup, AuthorGroupMember, Report, UserAuthor
from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.sources import openalex_source as oa

_AUTHOR_ID = "A5023888391"
_AUTHOR_ID_2 = "A9999999999"


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def _author_json(name="Jane Smith", author_id=_AUTHOR_ID):
    return {
        "id": f"https://openalex.org/{author_id}",
        "display_name": name,
        "orcid": None,
        "works_count": 10,
        "cited_by_count": 100,
    }


def _search_payload(candidates):
    """`candidates`: list of (author_id, name, institution, works, cites)."""
    return {
        "results": [
            {
                "id": f"https://openalex.org/{author_id}",
                "display_name": name,
                "orcid": None,
                "works_count": works,
                "cited_by_count": cites,
                "last_known_institutions": ([{"display_name": institution}] if institution else []),
                "topics": [{"display_name": "Immunology"}],
            }
            for author_id, name, institution, works, cites in candidates
        ]
    }


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(oa, "openalex_slot", lambda: True)


def _make_user(db, suffix):
    user = User.query.filter_by(email=f"agui{suffix}@example.test").first()
    if user is None:
        user = User(
            username=f"agui{suffix}",
            email=f"agui{suffix}@example.test",
            full_name=f"Author Group UI User {suffix}",
            password_hash=generate_password_hash("password123"),
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
    return user


def _cleanup_user(db, uid):
    db.session.rollback()
    db.session.execute(
        text(
            "DELETE FROM author_group_members WHERE group_id IN "
            "(SELECT id FROM author_groups WHERE user_id = :uid)"
        ),
        {"uid": uid},
    )
    db.session.execute(text("DELETE FROM author_groups WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM reports WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM user_authors WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


@pytest.fixture
def a_user(db):
    user = _make_user(db, "a")
    uid = user.id
    yield user
    _cleanup_user(db, uid)


@pytest.fixture
def other_user(db):
    user = _make_user(db, "b")
    uid = user.id
    yield user
    _cleanup_user(db, uid)


@pytest.fixture
def logged_in(client, a_user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(a_user.id)
        sess["_fresh"] = True
    return client


def _stub_report_delay(monkeypatch):
    calls = []

    class _FakeAsyncResult:
        id = "fake-task-id"

    def _fake_delay(report_id):
        calls.append(report_id)
        return _FakeAsyncResult()

    monkeypatch.setattr(report_tasks_module.generate, "delay", _fake_delay)
    return calls


# ----------------------------------------------------------------------------
# Name search
# ----------------------------------------------------------------------------


def test_search_lists_candidates_with_institution_and_counts(logged_in, monkeypatch):
    monkeypatch.setattr(
        oa._session,
        "get",
        lambda *a, **k: _Resp(
            _search_payload([(_AUTHOR_ID, "Jane Smith", "Example University", 42, 999)])
        ),
    )
    resp = logged_in.post("/papers/profile/authors/search", data={"name": "Jane Smith"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Jane Smith" in body
    assert "Example University" in body
    assert "42" in body
    assert "999" in body


def test_search_with_no_results_shows_a_message(logged_in, monkeypatch):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp({"results": []}))
    resp = logged_in.post("/papers/profile/authors/search", data={"name": "Nobody Findable"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No authors found" in body or "bulunamadı" in body.lower()


def test_search_rejects_empty_name(logged_in, db, a_user):
    resp = logged_in.post("/papers/profile/authors/search", data={"name": ""})
    assert resp.status_code == 200
    assert UserAuthor.query.filter_by(user_id=a_user.id).count() == 0


def test_search_throttled_shows_a_message_not_500(logged_in, monkeypatch):
    def _boom(*a, **k):
        raise SourceThrottledError("openalex rate limit")

    monkeypatch.setattr(oa, "search_authors", _boom)
    resp = logged_in.post("/papers/profile/authors/search", data={"name": "Jane Smith"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "busy" in body.lower() or "meşgul" in body.lower() or "yoğun" in body.lower()


def test_search_login_is_required(client):
    resp = client.post("/papers/profile/authors/search", data={"name": "Jane Smith"})
    assert resp.status_code in (301, 302)


# ----------------------------------------------------------------------------
# Following a search candidate — reuses submit_author_follow, keyed by
# OpenAlex id (never an auto-pick — see ADR-0003).
# ----------------------------------------------------------------------------


def test_following_a_candidate_uses_its_openalex_id(logged_in, db, a_user, monkeypatch):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    resp = logged_in.post("/papers/profile/authors/follow", data={"identifier": _AUTHOR_ID})
    assert resp.status_code == 200
    row = UserAuthor.query.filter_by(user_id=a_user.id).one()
    assert row.openalex_id == _AUTHOR_ID
    assert row.active is True  # a direct follow feeds the nightly ingest


# ----------------------------------------------------------------------------
# Adding a search candidate straight to a group
# ----------------------------------------------------------------------------


def test_adding_a_new_author_via_search_to_a_group_is_paused(logged_in, db, a_user, monkeypatch):
    """Regression gate: a brand-new UserAuthor created only to join a group
    must not start feeding the nightly ingest."""
    group, error = service.create_author_group(a_user, "Committee")
    assert error is None

    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    resp = logged_in.post(
        "/papers/profile/authors/search/add-to-group",
        data={"openalex_id": _AUTHOR_ID, "group_id": str(group.id)},
    )
    assert resp.status_code == 200

    row = UserAuthor.query.filter_by(user_id=a_user.id, openalex_id=_AUTHOR_ID).one()
    assert row.active is False
    assert service.count_group_members(group) == 1


def test_adding_an_already_followed_author_to_a_group_does_not_resurrect_a_pause(
    logged_in, db, a_user, monkeypatch
):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    row, error = service.follow_author(a_user, _AUTHOR_ID)
    assert error is None
    service.toggle_user_author(a_user, row.id)
    db.session.refresh(row)
    assert row.active is False

    group, _ = service.create_author_group(a_user, "Committee")
    resp = logged_in.post(
        "/papers/profile/authors/search/add-to-group",
        data={"openalex_id": _AUTHOR_ID, "group_id": str(group.id)},
    )
    assert resp.status_code == 200
    db.session.refresh(row)
    assert row.active is False


def test_add_to_group_with_missing_fields_does_not_500(logged_in, db, a_user):
    resp = logged_in.post(
        "/papers/profile/authors/search/add-to-group", data={"openalex_id": "", "group_id": ""}
    )
    assert resp.status_code == 200


def test_add_to_group_login_is_required(client):
    resp = client.post(
        "/papers/profile/authors/search/add-to-group",
        data={"openalex_id": _AUTHOR_ID, "group_id": "1"},
    )
    assert resp.status_code in (301, 302)


# ----------------------------------------------------------------------------
# Group CRUD
# ----------------------------------------------------------------------------


def test_create_rename_delete_group_round_trip(logged_in, db, a_user):
    resp = logged_in.post(
        "/papers/profile/authors/groups/create",
        data={"name": "Immunology watch", "description": "key people"},
    )
    assert resp.status_code == 200
    group = AuthorGroup.query.filter_by(user_id=a_user.id).one()
    assert group.name == "Immunology watch"

    resp = logged_in.post(
        f"/papers/profile/authors/groups/{group.id}/rename", data={"name": "Renamed"}
    )
    assert resp.status_code == 200
    db.session.refresh(group)
    assert group.name == "Renamed"

    resp = logged_in.post(f"/papers/profile/authors/groups/{group.id}/delete")
    assert resp.status_code == 200
    assert AuthorGroup.query.filter_by(user_id=a_user.id).count() == 0


def test_create_group_shows_a_message_on_blank_name(logged_in, db, a_user):
    resp = logged_in.post(
        "/papers/profile/authors/groups/create", data={"name": "", "description": ""}
    )
    assert resp.status_code == 200
    assert AuthorGroup.query.filter_by(user_id=a_user.id).count() == 0


def test_group_cap_shows_a_message(logged_in, db, a_user, monkeypatch):
    monkeypatch.setattr(service, "MAX_AUTHOR_GROUPS", 1)
    resp = logged_in.post(
        "/papers/profile/authors/groups/create", data={"name": "First", "description": ""}
    )
    assert resp.status_code == 200
    assert AuthorGroup.query.filter_by(user_id=a_user.id).count() == 1

    resp = logged_in.post(
        "/papers/profile/authors/groups/create", data={"name": "Second", "description": ""}
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert AuthorGroup.query.filter_by(user_id=a_user.id).count() == 1
    assert "limit" in body.lower() or "sınır" in body.lower() or "dolu" in body.lower()


def test_member_cap_shows_a_message(logged_in, db, a_user, monkeypatch):
    monkeypatch.setattr(service, "MAX_GROUP_MEMBERS", 1)
    group, _ = service.create_author_group(a_user, "Small group")

    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json("A", _AUTHOR_ID)))
    author1, _ = service.follow_author(a_user, _AUTHOR_ID)
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json("B", _AUTHOR_ID_2)))
    author2, _ = service.follow_author(a_user, _AUTHOR_ID_2)

    resp = logged_in.post(
        f"/papers/profile/authors/groups/{group.id}/members/add",
        data={"user_author_id": str(author1.id)},
    )
    assert resp.status_code == 200
    assert service.count_group_members(group) == 1

    resp = logged_in.post(
        f"/papers/profile/authors/groups/{group.id}/members/add",
        data={"user_author_id": str(author2.id)},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert service.count_group_members(group) == 1
    assert "full" in body.lower() or "dolu" in body.lower()


# ----------------------------------------------------------------------------
# Members — add existing / remove
# ----------------------------------------------------------------------------


def test_add_existing_followed_author_and_remove(logged_in, db, a_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    author, _ = service.follow_author(a_user, _AUTHOR_ID, activate=False)

    resp = logged_in.post(
        f"/papers/profile/authors/groups/{group.id}/members/add",
        data={"user_author_id": str(author.id)},
    )
    assert resp.status_code == 200
    assert service.count_group_members(group) == 1

    members = service.list_group_members(group)
    assert len(members) == 1
    membership = AuthorGroupMember.query.filter_by(
        group_id=group.id, user_author_id=author.id
    ).one()

    resp = logged_in.post(
        f"/papers/profile/authors/groups/{group.id}/members/{membership.id}/remove"
    )
    assert resp.status_code == 200
    assert service.count_group_members(group) == 0


def test_group_member_name_appears_in_rendered_tab(logged_in, db, a_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    author, _ = service.follow_author(a_user, _AUTHOR_ID, activate=False)
    service.add_group_member(a_user, group.id, author.id)

    body = logged_in.get("/settings/profile?tab=authors").get_data(as_text=True)
    assert author.author_name in body


# ----------------------------------------------------------------------------
# Ownership boundaries — someone else's group is a 404, not a silent no-op
# ----------------------------------------------------------------------------


def test_acting_on_another_users_group_is_404(logged_in, db, a_user, other_user, monkeypatch):
    group, _ = service.create_author_group(other_user, "Not yours")
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    author, _ = service.follow_author(other_user, _AUTHOR_ID, activate=False)
    service.add_group_member(other_user, group.id, author.id)
    membership = AuthorGroupMember.query.filter_by(group_id=group.id).one()

    assert (
        logged_in.post(
            f"/papers/profile/authors/groups/{group.id}/rename", data={"name": "x"}
        ).status_code
        == 404
    )
    assert logged_in.post(f"/papers/profile/authors/groups/{group.id}/delete").status_code == 404
    assert (
        logged_in.post(
            f"/papers/profile/authors/groups/{group.id}/members/add",
            data={"user_author_id": str(author.id)},
        ).status_code
        == 404
    )
    assert (
        logged_in.post(
            f"/papers/profile/authors/groups/{group.id}/members/{membership.id}/remove"
        ).status_code
        == 404
    )
    assert (
        logged_in.post(
            f"/papers/profile/authors/groups/{group.id}/report", data={"years": "5"}
        ).status_code
        == 404
    )

    assert AuthorGroup.query.get(group.id).name == "Not yours"


def test_group_routes_require_login(client):
    assert client.post("/papers/profile/authors/groups/create", data={"name": "x"}).status_code in (
        301,
        302,
    )
    assert client.post(
        "/papers/profile/authors/groups/1/rename", data={"name": "x"}
    ).status_code in (
        301,
        302,
    )
    assert client.post("/papers/profile/authors/groups/1/delete").status_code in (301, 302)
    assert client.post("/papers/profile/authors/groups/1/report").status_code in (301, 302)


# ----------------------------------------------------------------------------
# "Generate a file for this group" — queues a report, does not run it
# ----------------------------------------------------------------------------


def test_generate_report_for_group_queues_the_task(logged_in, db, a_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    author, _ = service.follow_author(a_user, _AUTHOR_ID, activate=False)
    service.add_group_member(a_user, group.id, author.id)

    calls = _stub_report_delay(monkeypatch)

    resp = logged_in.post(f"/papers/profile/authors/groups/{group.id}/report", data={"years": "5"})
    assert resp.status_code in (301, 302)

    report = Report.query.filter_by(user_id=a_user.id, kind="author_group").one()
    assert report.params["group_id"] == group.id
    assert report.params["years"] == 5
    assert calls == [report.id]


def test_generate_report_htmx_returns_202_with_redirect(logged_in, db, a_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    author, _ = service.follow_author(a_user, _AUTHOR_ID, activate=False)
    service.add_group_member(a_user, group.id, author.id)
    _stub_report_delay(monkeypatch)

    resp = logged_in.post(
        f"/papers/profile/authors/groups/{group.id}/report",
        data={"years": "5"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 202
    report = Report.query.filter_by(user_id=a_user.id, kind="author_group").one()
    assert resp.headers.get("HX-Redirect") == f"/papers/reports/{report.id}"


def test_generate_report_for_an_empty_group_shows_a_message_and_does_not_queue(
    logged_in, db, a_user, monkeypatch
):
    group, _ = service.create_author_group(a_user, "Empty")
    calls = _stub_report_delay(monkeypatch)

    resp = logged_in.post(f"/papers/profile/authors/groups/{group.id}/report", data={"years": "5"})
    assert resp.status_code == 200
    assert Report.query.filter_by(user_id=a_user.id).count() == 0
    assert calls == []
