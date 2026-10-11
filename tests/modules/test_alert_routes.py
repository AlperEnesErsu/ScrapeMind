"""The saved-search and Zotero routes, through HTTP.

tests/modules/test_alerts.py and test_zotero.py exercise the services; this
file is about what sits in front of them: who may touch which row, what a bad
form does, and where each action sends the user afterwards. Ownership is the
one that matters -- every id in these URLs is guessable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.models.audit import AuditLog
from app.core.models.user import User
from app.extensions import db as _db
from app.modules.scrape import alert_routes
from app.modules.scrape.models import Paper, SavedSearch, UserPaper


def _flashes(client) -> list[tuple[str, str]]:
    with client.session_transaction() as sess:
        return list(sess.get("_flashes", []))


def _searches(uid: int) -> list[SavedSearch]:
    _db.session.expire_all()
    return SavedSearch.query.filter_by(user_id=uid).order_by(SavedSearch.id).all()


@pytest.fixture
def auth_client(auth_client, db):
    """conftest's logged-in client, reading English, so the flashed messages
    below can be compared with their msgids."""
    _client, uid = auth_client
    db.session.get(User, uid).locale = "en"
    db.session.commit()
    return auth_client


@pytest.fixture
def stranger(db):
    """Somebody else, with a saved search of their own."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"stranger-{suffix}",
        email=f"stranger-{suffix}@example.test",
        full_name="Stranger",
        password_hash="x",
    )
    db.session.add(user)
    db.session.flush()
    search = SavedSearch(user_id=user.id, name="Not yours", q="pomegranate", cadence="weekly")
    db.session.add(search)
    db.session.commit()
    yield search.id
    db.session.rollback()
    # saved_searches cascades with its user.
    db.session.query(User).filter_by(id=user.id).delete()
    db.session.commit()


@pytest.fixture
def own_search(db, auth_client):
    _client, uid = auth_client

    def _make(name: str = "Mine", **kwargs) -> int:
        search = SavedSearch(user_id=uid, name=name, q=kwargs.pop("q", "graphene"), **kwargs)
        db.session.add(search)
        db.session.commit()
        return search.id

    return _make


# --------------------------------------------------------------------------
# Creating
# --------------------------------------------------------------------------


def test_anonymous_cannot_create(client, db):
    r = client.post("/library/saved-searches", data={"name": "x"})
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]


def test_create_saves_the_search_on_screen(auth_client, db):
    client, uid = auth_client
    r = client.post(
        "/library/saved-searches",
        data={
            "name": "  Graphene  ",
            "q": " graphene batteries ",
            "cadence": "daily",
            "semantic": "1",
            "source": "arxiv",
            "quartile": "q1",
            "from": "2024-01-01",
            "to": "",
            "has_notes": "1",
        },
    )

    assert r.status_code == 302
    assert r.headers["Location"].endswith("/library/search")
    [search] = _searches(uid)
    assert search.name == "Graphene"
    assert search.q == "graphene batteries"
    assert search.cadence == "daily"
    assert search.semantic is True
    # Upper-cased, and the empty `to` is dropped rather than stored as "".
    assert search.filters == {
        "source": "arxiv",
        "quartile": "Q1",
        "date_from": "2024-01-01",
        "has_notes": True,
    }
    assert ("success", "Saved. You will be told when new papers match it.") in _flashes(client)
    assert (
        AuditLog.query.filter_by(
            action="saved_search.create", entity_id=str(search.id), user_id=uid
        ).count()
        == 1
    )


def test_create_normalises_what_it_does_not_recognise(auth_client, db):
    client, uid = auth_client
    client.post(
        "/library/saved-searches",
        data={"name": "Loose", "cadence": "hourly", "quartile": "Q9"},
        headers={"Referer": "http://localhost/library/search?q=x"},
    )

    [search] = _searches(uid)
    assert search.cadence == "weekly", "an unknown cadence falls back, it is not stored"
    assert search.q is None
    assert search.filters is None, "an unknown quartile is dropped, leaving nothing"
    assert search.semantic is False


def test_create_without_a_name_saves_nothing(auth_client, db):
    client, uid = auth_client
    r = client.post(
        "/library/saved-searches",
        data={"name": "   ", "q": "graphene"},
        headers={"Referer": "http://localhost/library/search?q=graphene"},
    )

    assert r.status_code == 302
    assert r.headers["Location"] == "http://localhost/library/search?q=graphene"
    assert _searches(uid) == []
    assert ("warning", "Give the saved search a name.") in _flashes(client)


def test_create_refuses_a_second_search_with_the_same_name(auth_client, own_search, db):
    client, uid = auth_client
    own_search("Graphene")

    client.post("/library/saved-searches", data={"name": "Graphene", "q": "other"})

    assert len(_searches(uid)) == 1
    [(category, _message)] = _flashes(client)
    assert category == "warning"


def test_the_same_name_is_free_for_another_user(auth_client, stranger, db):
    """Names clash per user, not globally -- the stranger's "Not yours" must
    not block this user from using it."""
    client, uid = auth_client
    client.post("/library/saved-searches", data={"name": "Not yours"})
    assert [s.name for s in _searches(uid)] == ["Not yours"]


def test_create_stops_at_the_ceiling(auth_client, own_search, db, monkeypatch):
    client, uid = auth_client
    monkeypatch.setattr(alert_routes, "MAX_SAVED_SEARCHES", 2)
    own_search("One")
    own_search("Two")

    client.post("/library/saved-searches", data={"name": "Three"})

    assert [s.name for s in _searches(uid)] == ["One", "Two"]
    [(category, message)] = _flashes(client)
    assert category == "warning"
    assert "2" in message


# --------------------------------------------------------------------------
# Changing and deleting -- the ownership checks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cadence", ["daily", "off"])
def test_set_cadence_changes_it(auth_client, own_search, db, cadence):
    client, uid = auth_client
    sid = own_search()

    r = client.post(f"/library/saved-searches/{sid}/cadence", data={"cadence": cadence})

    assert r.status_code == 302
    assert r.headers["Location"].endswith("/settings/workspace?tab=alerts")
    [search] = _searches(uid)
    assert search.cadence == cadence
    assert AuditLog.query.filter_by(action="saved_search.set_cadence", entity_id=str(sid)).count()


def test_set_cadence_rejects_an_unknown_value(auth_client, own_search, db):
    client, uid = auth_client
    sid = own_search()

    r = client.post(f"/library/saved-searches/{sid}/cadence", data={"cadence": "hourly"})

    assert r.status_code == 400
    assert _searches(uid)[0].cadence == "weekly"


def test_another_users_search_cannot_be_retimed(auth_client, stranger, db):
    client, _uid = auth_client

    r = client.post(f"/library/saved-searches/{stranger}/cadence", data={"cadence": "off"})

    # 404, not 403: whether the id exists is itself not this user's business.
    assert r.status_code == 404
    _db.session.expire_all()
    assert _db.session.get(SavedSearch, stranger).cadence == "weekly"


def test_delete_removes_the_search(auth_client, own_search, db):
    client, uid = auth_client
    keep = own_search("Keep")
    gone = own_search("Gone")

    r = client.post(f"/library/saved-searches/{gone}/delete")

    assert r.status_code == 302
    assert r.headers["Location"].endswith("/settings/workspace?tab=alerts")
    assert [s.id for s in _searches(uid)] == [keep]
    assert AuditLog.query.filter_by(action="saved_search.delete", entity_id=str(gone)).count()


def test_another_users_search_cannot_be_deleted(auth_client, stranger, db):
    client, _uid = auth_client

    assert client.post(f"/library/saved-searches/{stranger}/delete").status_code == 404
    _db.session.expire_all()
    assert _db.session.get(SavedSearch, stranger) is not None


def test_a_missing_search_is_404(auth_client, db):
    client, _uid = auth_client
    assert client.post("/library/saved-searches/999999/delete").status_code == 404
    assert (
        client.post("/library/saved-searches/999999/cadence", data={"cadence": "off"}).status_code
        == 404
    )


# --------------------------------------------------------------------------
# The settings tab
# --------------------------------------------------------------------------


def test_the_tab_lists_only_the_users_own_searches(auth_client, own_search, stranger, db):
    client, _uid = auth_client
    own_search("Graphene watch", q="graphene")

    body = client.get("/settings/workspace?tab=alerts").get_data(as_text=True)

    assert "Graphene watch" in body
    assert "Not yours" not in body


def test_the_tab_is_empty_without_searches(auth_client, db):
    client, _uid = auth_client
    body = client.get("/settings/workspace?tab=alerts").get_data(as_text=True)
    assert "No saved searches yet." in body


def test_a_broken_search_still_renders_so_it_can_be_deleted(
    auth_client, own_search, db, monkeypatch
):
    client, _uid = auth_client
    sid = own_search("Broken")

    def _boom(search):
        raise RuntimeError("bad query")

    monkeypatch.setattr(alert_routes, "find_new_matches", _boom)
    r = client.get("/settings/workspace?tab=alerts")

    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Broken" in body
    assert f"/library/saved-searches/{sid}/delete" in body


# --------------------------------------------------------------------------
# Zotero
# --------------------------------------------------------------------------


def test_a_zotero_key_without_a_user_id_is_refused(auth_client, db):
    from app.modules.scrape.zotero import get_credentials

    client, uid = auth_client
    r = client.post("/library/zotero/credentials", data={"zotero_api_key": "k" * 24})

    assert r.status_code == 302
    assert r.headers["Location"].endswith("/settings/workspace?tab=zotero")
    assert get_credentials(_db.session.get(User, uid)) is None
    [(category, _message)] = _flashes(client)
    assert category == "warning"


def test_zotero_credentials_are_stored_then_cleared(auth_client, db):
    from app.modules.scrape.zotero import get_credentials

    client, uid = auth_client
    client.post(
        "/library/zotero/credentials",
        data={"zotero_api_key": "k" * 24, "zotero_user_id": " 123456 "},
    )
    _db.session.expire_all()
    assert get_credentials(_db.session.get(User, uid)) == ("k" * 24, "123456")
    assert AuditLog.query.filter_by(action="zotero.credentials_set", user_id=uid).count() == 1

    # The tab says it is connected and shows the id -- never the key.
    body = client.get("/settings/workspace?tab=zotero").get_data(as_text=True)
    assert "123456" in body
    assert "k" * 24 not in body

    client.post("/library/zotero/credentials", data={"zotero_api_key": ""})
    _db.session.expire_all()
    assert get_credentials(_db.session.get(User, uid)) is None
    assert AuditLog.query.filter_by(action="zotero.credentials_cleared", user_id=uid).count() == 1
    assert ("success", "Zotero disconnected.") in _flashes(client)


@pytest.fixture
def library_paper(db, auth_client):
    _client, uid = auth_client
    paper = Paper(
        source="openalex",
        external_id=f"W{uuid.uuid4().hex[:12]}",
        title="Pomegranate heat spreaders",
        published_at=datetime.now(UTC),
    )
    db.session.add(paper)
    db.session.flush()
    db.session.add(UserPaper(user_id=uid, paper_id=paper.id))
    db.session.commit()
    yield paper.id
    db.session.rollback()
    db.session.query(UserPaper).filter_by(paper_id=paper.id).delete()
    db.session.query(Paper).filter_by(id=paper.id).delete()
    db.session.commit()


def test_zotero_export_of_an_unknown_view_is_404(auth_client, db):
    client, _uid = auth_client
    assert client.post("/library/zotero/export", data={"view": "everything"}).status_code == 404


def test_zotero_export_sends_the_library_and_reports_counts(
    auth_client, library_paper, db, monkeypatch
):
    from app.modules.scrape import zotero

    client, uid = auth_client
    sent = {}

    def _export(user, papers):
        sent["user"] = user.id
        sent["papers"] = [p.id for p in papers]
        return zotero.ExportResult(created=1)

    monkeypatch.setattr(zotero, "export_papers", _export)
    r = client.post("/library/zotero/export", data={"view": "all"})

    assert r.status_code == 302
    assert r.headers["Location"].endswith("/library/")
    assert sent == {"user": uid, "papers": [library_paper]}
    assert ("success", "Sent to Zotero: 1 new, 0 updated.") in _flashes(client)
    assert AuditLog.query.filter_by(action="zotero.export", user_id=uid).count() == 1


def test_a_partial_zotero_export_says_how_much_failed(auth_client, db, monkeypatch):
    from app.modules.scrape import zotero

    client, _uid = auth_client
    monkeypatch.setattr(
        zotero,
        "export_papers",
        lambda user, papers: zotero.ExportResult(created=30, updated=7, failed=3),
    )
    client.post(
        "/library/zotero/export",
        data={"view": "favorites"},
        headers={"Referer": "http://localhost/library/?view=favorites"},
    )

    assert ("warning", "Sent to Zotero: 37 of 40. 3 failed.") in _flashes(client)


def test_a_failed_zotero_export_is_reported_not_audited(auth_client, db, monkeypatch):
    from app.modules.scrape import zotero

    client, uid = auth_client

    def _refuse(user, papers):
        raise zotero.ZoteroError("key refused")

    monkeypatch.setattr(zotero, "export_papers", _refuse)
    r = client.post("/library/zotero/export", data={"view": "read_later"})

    assert r.status_code == 302
    assert ("danger", "Zotero export failed: key refused") in _flashes(client)
    assert AuditLog.query.filter_by(action="zotero.export", user_id=uid).count() == 0
