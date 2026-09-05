"""The Followed Authors profile tab (Faz 5.4)."""

from __future__ import annotations

import pytest
import requests
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.modules.scrape.models import UserAuthor
from app.modules.scrape.sources import openalex_source as oa

_ORCID = "0000-0002-1825-0097"
_AUTHOR_ID = "A5023888391"


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def _author_json(name="Jane Smith"):
    return {
        "id": f"https://openalex.org/{_AUTHOR_ID}",
        "display_name": name,
        "orcid": f"https://orcid.org/{_ORCID}",
    }


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(oa, "openalex_slot", lambda: True)


@pytest.fixture
def orcid_type(db):
    """`add_identifier` refuses an unknown type code, and the test schema has
    no seed rows — see scripts/seed.py."""
    from app.modules.academic.models import IdentifierType

    row = IdentifierType.query.filter_by(code="orcid").first()
    if row is None:
        row = IdentifierType(
            code="orcid",
            name="ORCID",
            validation_regex=r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$",
            verification_method="manual",
        )
        db.session.add(row)
        db.session.commit()
    return row


@pytest.fixture
def a_user(db):
    user = User.query.filter_by(email="authorui@example.test").first()
    if user is None:
        user = User(
            username="authoruiuser",
            email="authorui@example.test",
            full_name="Author UI User",
            password_hash=generate_password_hash("password123"),
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
    uid = user.id

    yield user

    db.session.rollback()
    db.session.execute(text("DELETE FROM user_authors WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM user_identifiers WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


@pytest.fixture
def logged_in(client, a_user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(a_user.id)
        sess["_fresh"] = True
    return client


def _follow(logged_in, monkeypatch, identifier=_ORCID, payload=None):
    monkeypatch.setattr(
        oa._session, "get", lambda *a, **k: _Resp(payload if payload else _author_json())
    )
    return logged_in.post("/papers/profile/authors/follow", data={"identifier": identifier})


# ----------------------------------------------------------------------------
# The tab
# ----------------------------------------------------------------------------


def test_tab_is_registered(app):
    """`app` is required: the tab is registered as a side effect of importing
    `scrape.routes`, which only happens when the app is created."""
    from app.core.settings.tab_registry import all_tabs

    assert "authors" in {code for code, *_ in all_tabs()}


def test_tab_renders_empty_state(logged_in):
    body = logged_in.get("/settings/profile?tab=authors").get_data(as_text=True)
    assert "not following anyone" in body or "kimseyi takip etmiyorsunuz" in body


# ----------------------------------------------------------------------------
# Follow
# ----------------------------------------------------------------------------


def test_follow_adds_the_author(logged_in, db, a_user, monkeypatch):
    resp = _follow(logged_in, monkeypatch)
    assert resp.status_code == 200
    assert "Jane Smith" in resp.get_data(as_text=True)
    assert UserAuthor.query.filter_by(user_id=a_user.id).count() == 1


def test_follow_reports_an_unknown_identifier(logged_in, db, a_user, monkeypatch):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(status_code=404))
    resp = logged_in.post("/papers/profile/authors/follow", data={"identifier": _ORCID})

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No author found" in body or "yazar bulunamadı" in body
    assert UserAuthor.query.filter_by(user_id=a_user.id).count() == 0


def test_follow_rejects_an_empty_identifier(logged_in, db, a_user):
    resp = logged_in.post("/papers/profile/authors/follow", data={"identifier": ""})
    assert resp.status_code == 200
    assert UserAuthor.query.filter_by(user_id=a_user.id).count() == 0


def test_login_is_required(client):
    resp = client.post("/papers/profile/authors/follow", data={"identifier": _ORCID})
    assert resp.status_code in (301, 302)


# ----------------------------------------------------------------------------
# Own-ORCID shortcut
# ----------------------------------------------------------------------------


def test_own_orcid_is_offered_as_a_one_click_follow(logged_in, db, a_user, orcid_type):
    """Otherwise this is a copy-paste between the Identifiers tab and here."""
    from app.modules.academic.service import add_identifier

    add_identifier(a_user, "orcid", _ORCID)

    body = logged_in.get("/settings/profile?tab=authors").get_data(as_text=True)
    assert _ORCID in body


def test_own_orcid_disappears_once_followed(logged_in, db, a_user, orcid_type, monkeypatch):
    """Offering "follow yourself" to someone who already does is noise."""
    from app.modules.academic.service import add_identifier

    add_identifier(a_user, "orcid", _ORCID)
    _follow(logged_in, monkeypatch)

    body = logged_in.get("/settings/profile?tab=authors").get_data(as_text=True)
    assert "Follow your own publications" not in body
    assert "Kendi yayınlarınızı takip edin" not in body


# ----------------------------------------------------------------------------
# Pause / unfollow
# ----------------------------------------------------------------------------


def test_pause_keeps_the_row(logged_in, db, a_user, monkeypatch):
    """Deleting and re-adding would lose last_work_at and re-import the
    author's back catalogue."""
    _follow(logged_in, monkeypatch)
    row = UserAuthor.query.filter_by(user_id=a_user.id).one()

    logged_in.post(f"/papers/profile/authors/{row.id}/pause")
    db.session.refresh(row)
    assert row.active is False
    assert UserAuthor.query.filter_by(user_id=a_user.id).count() == 1


def test_unfollow_removes_the_row(logged_in, db, a_user, monkeypatch):
    _follow(logged_in, monkeypatch)
    row = UserAuthor.query.filter_by(user_id=a_user.id).one()

    resp = logged_in.post(f"/papers/profile/authors/{row.id}/delete")
    assert resp.status_code == 200
    assert UserAuthor.query.filter_by(user_id=a_user.id).count() == 0


def test_acting_on_another_users_follow_is_404(logged_in, db, a_user):
    """Ownership is enforced on the route, not just in the service.

    The row is created directly for a second user rather than by driving a
    second test client — swapping `_user_id` on a live client does not
    actually change who Flask-Login resolves, so that shape tests nothing.
    """
    other = User(
        username="otherauthoruiuser",
        email="otherauthorui@example.test",
        full_name="Other",
        password_hash=generate_password_hash("x"),
        is_active=True,
    )
    db.session.add(other)
    db.session.commit()
    foreign = UserAuthor(
        user_id=other.id, author_name="Someone Else", openalex_id="A999", active=True
    )
    db.session.add(foreign)
    db.session.commit()

    try:
        assert logged_in.post(f"/papers/profile/authors/{foreign.id}/delete").status_code == 404
        assert logged_in.post(f"/papers/profile/authors/{foreign.id}/pause").status_code == 404
        assert UserAuthor.query.filter_by(id=foreign.id).count() == 1
    finally:
        db.session.delete(foreign)
        db.session.delete(other)
        db.session.commit()
