"""Collections — CRUD, membership, ownership, export."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape import collections_service as cs
from app.modules.scrape.models import Collection, Paper, UserPaper


@pytest.fixture
def ctx(db):
    for tbl in (
        "collection_papers",
        "collections",
        "user_papers",
        "papers",
        "audit_logs",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter(User.username.in_(["collector", "rival"])).delete(
        synchronize_session=False
    )
    db.session.commit()

    users = {}
    for name in ("collector", "rival"):
        u = User(
            username=name,
            email=f"{name}@example.test",
            full_name=name.title(),
            password_hash=LocalAuthStrategy.hash_password("x12345678"),
            is_active=True,
        )
        db.session.add(u)
        users[name] = u
    db.session.commit()

    p1 = Paper(
        source="arxiv",
        external_id="c1",
        title="Paper One",
        abstract="a",
        authors=["A"],
        published_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    p2 = Paper(
        source="pubmed",
        external_id="c2",
        title="Paper Two",
        abstract="b",
        authors=["B"],
        published_at=datetime(2025, 2, 1, tzinfo=UTC),
    )
    db.session.add_all([p1, p2])
    db.session.commit()
    up1 = UserPaper(user_id=users["collector"].id, paper_id=p1.id, matched_keyword="k")
    up2 = UserPaper(user_id=users["collector"].id, paper_id=p2.id, matched_keyword="k")
    rival_up = UserPaper(user_id=users["rival"].id, paper_id=p1.id, matched_keyword="k")
    db.session.add_all([up1, up2, rival_up])
    db.session.commit()

    data = {
        "collector": users["collector"],
        "rival": users["rival"],
        "up1": up1.id,
        "up2": up2.id,
        "rival_up": rival_up.id,
    }
    yield data

    db.session.rollback()
    for tbl in (
        "collection_papers",
        "collections",
        "user_papers",
        "papers",
        "audit_logs",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter(User.username.in_(["collector", "rival"])).delete(
        synchronize_session=False
    )
    db.session.commit()


def _login(client, user):
    with client.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_create_and_list(db, ctx):
    coll, err = cs.create_collection(ctx["collector"], "Thesis")
    assert err is None and coll.id
    assert [c.name for c in cs.list_collections(ctx["collector"])] == ["Thesis"]


def test_create_rejects_blank_and_duplicate(db, ctx):
    assert cs.create_collection(ctx["collector"], "  ")[1] == "Collection name is required."
    cs.create_collection(ctx["collector"], "Dup")
    assert cs.create_collection(ctx["collector"], "Dup")[1] is not None


def test_same_name_ok_for_different_users(db, ctx):
    assert cs.create_collection(ctx["collector"], "Shared")[1] is None
    # rival can use the same name — uniqueness is per-user.
    assert cs.create_collection(ctx["rival"], "Shared")[1] is None


def test_add_paper_is_idempotent(db, ctx):
    coll, _ = cs.create_collection(ctx["collector"], "C")
    assert cs.add_paper(coll, ctx["collector"], ctx["up1"]) == (True, None)
    assert cs.add_paper(coll, ctx["collector"], ctx["up1"]) == (True, None)  # no dup
    assert len(coll.papers) == 1


def test_add_rejects_foreign_paper(db, ctx):
    coll, _ = cs.create_collection(ctx["collector"], "C")
    # rival's user_paper must not be addable to collector's collection.
    ok, err = cs.add_paper(coll, ctx["collector"], ctx["rival_up"])
    assert ok is False and err == "not_found"
    assert coll.papers == []


def test_remove_and_membership_lookup(db, ctx):
    coll, _ = cs.create_collection(ctx["collector"], "C")
    cs.add_paper(coll, ctx["collector"], ctx["up1"])
    cs.add_paper(coll, ctx["collector"], ctx["up2"])
    assert cs.collection_ids_for_paper(ctx["collector"], ctx["up1"]) == {coll.id}
    cs.remove_paper(coll, ctx["up1"])
    assert cs.collection_ids_for_paper(ctx["collector"], ctx["up1"]) == set()


def test_get_collection_is_user_scoped(db, ctx):
    coll, _ = cs.create_collection(ctx["collector"], "Mine")
    assert cs.get_collection(ctx["rival"], coll.id) is None  # not rival's


def test_delete_empties_and_soft_deletes(db, ctx):
    coll, _ = cs.create_collection(ctx["collector"], "C")
    cs.add_paper(coll, ctx["collector"], ctx["up1"])
    cid = coll.id
    cs.delete_collection(coll)
    assert cs.get_collection(ctx["collector"], cid) is None
    # The paper itself survives — only the collection membership is gone.
    assert UserPaper.query.filter_by(id=ctx["up1"]).first() is not None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_route_create_and_detail(client, ctx):
    _login(client, ctx["collector"])
    r = client.post("/library/collections", data={"name": "Route Coll"}, follow_redirects=False)
    assert r.status_code == 302
    coll = Collection.query.filter_by(user_id=ctx["collector"].id, name="Route Coll").first()
    assert coll is not None
    assert client.get(f"/library/collections/{coll.id}").status_code == 200


def test_route_add_via_htmx_toggles(client, ctx, db):
    coll, _ = cs.create_collection(ctx["collector"], "C")
    _login(client, ctx["collector"])
    r = client.post(
        f"/library/collections/{coll.id}/add/{ctx['up1']}",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    db.session.refresh(coll)
    assert len(coll.papers) == 1


def test_route_cannot_touch_foreign_collection(client, ctx):
    coll, _ = cs.create_collection(ctx["rival"], "Theirs")
    _login(client, ctx["collector"])
    assert client.get(f"/library/collections/{coll.id}").status_code == 404
    assert client.post(f"/library/collections/{coll.id}/add/{ctx['up1']}").status_code == 404


def test_route_export_collection(client, ctx):
    coll, _ = cs.create_collection(ctx["collector"], "Export Me")
    cs.add_paper(coll, ctx["collector"], ctx["up1"])
    _login(client, ctx["collector"])
    r = client.get(f"/library/collections/{coll.id}/export.bib")
    assert r.status_code == 200
    assert r.mimetype == "application/x-bibtex"
    assert "@misc" in r.get_data(as_text=True)


def test_collections_require_login(client):
    assert client.get("/library/collections", follow_redirects=False).status_code in (302, 401)
