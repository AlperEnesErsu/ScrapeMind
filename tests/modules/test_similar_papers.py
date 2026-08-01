"""Similar Papers panel — lazy-loaded internal (library) + external (S2)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape.models import Paper, UserPaper
from app.modules.scrape.sources.payload import PaperPayload


@pytest.fixture
def ctx(db):
    for tbl in ("paper_notes", "user_papers", "papers", "user_roles"):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter(User.username.in_(["simuser", "rival"])).delete(
        synchronize_session=False
    )
    db.session.commit()

    users = {}
    for name in ("simuser", "rival"):
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

    # Two papers with the same matched keyword → internal similarity.
    p_main = Paper(
        source="arxiv",
        external_id="s1",
        title="Main Paper",
        abstract="a",
        authors=["A"],
        categories=["cs.LG"],
        doi="10.1/main",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    p_sib = Paper(
        source="arxiv",
        external_id="s2",
        title="Sibling Paper",
        abstract="b",
        authors=["B"],
        categories=["cs.LG"],
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    db.session.add_all([p_main, p_sib])
    db.session.commit()
    main = UserPaper(user_id=users["simuser"].id, paper_id=p_main.id, matched_keyword="rl")
    sib = UserPaper(user_id=users["simuser"].id, paper_id=p_sib.id, matched_keyword="rl")
    rival_main = UserPaper(user_id=users["rival"].id, paper_id=p_main.id, matched_keyword="rl")
    db.session.add_all([main, sib, rival_main])
    db.session.commit()

    data = {"user": users["simuser"], "rival": users["rival"], "main_id": main.id}
    yield data

    db.session.rollback()
    for tbl in ("paper_notes", "user_papers", "papers", "user_roles"):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter(User.username.in_(["simuser", "rival"])).delete(
        synchronize_session=False
    )
    db.session.commit()


def _login(client, user):
    with client.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True


def test_similar_shows_internal_sibling(client, ctx, monkeypatch):
    # No external recs — internal (same-keyword) sibling still shows.
    monkeypatch.setattr(
        "app.modules.scrape.sources.semantic_scholar_source.fetch_similar_papers",
        lambda *a, **k: [],
    )
    _login(client, ctx["user"])
    r = client.get(f"/papers/{ctx['main_id']}/similar")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Sibling Paper" in body
    assert "Main Paper" not in body  # never recommend the paper itself


def test_similar_renders_external_recommendations(client, ctx, monkeypatch):
    rec = PaperPayload(
        source="semantic_scholar", external_id="ext1", title="External Rec Paper",
        abstract="x", authors=["Ada Lovelace"], url="https://example.com/p",
        pdf_url=None, published_at=None, categories=[],
    )  # fmt: skip
    monkeypatch.setattr(
        "app.modules.scrape.sources.semantic_scholar_source.fetch_similar_papers",
        lambda *a, **k: [rec],
    )
    _login(client, ctx["user"])
    body = client.get(f"/papers/{ctx['main_id']}/similar").get_data(as_text=True)
    assert "External Rec Paper" in body
    assert "https://example.com/p" in body


def test_similar_survives_s2_failure(client, ctx, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(
        "app.modules.scrape.sources.semantic_scholar_source.fetch_similar_papers", _boom
    )
    _login(client, ctx["user"])
    r = client.get(f"/papers/{ctx['main_id']}/similar")
    # A flaky S2 must not 500 the panel — internal results still render.
    assert r.status_code == 200
    assert "Sibling Paper" in r.get_data(as_text=True)


def test_similar_is_ownership_scoped(client, ctx):
    _login(client, ctx["rival"])  # rival owns a copy of main, but not this user_paper_id
    assert client.get(f"/papers/{ctx['main_id']}/similar").status_code == 404


def test_similar_requires_login(client, ctx):
    r = client.get(f"/papers/{ctx['main_id']}/similar", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_detail_page_lazy_loads_similar(client, ctx, monkeypatch):
    # The detail page itself must NOT call S2 (it's lazy) — it just embeds the
    # hx-get trigger pointing at /similar.
    monkeypatch.setattr(
        "app.modules.scrape.sources.semantic_scholar_source.fetch_similar_papers",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("S2 called inline!")),
    )
    _login(client, ctx["user"])
    body = client.get(f"/papers/{ctx['main_id']}").get_data(as_text=True)
    assert f"/papers/{ctx['main_id']}/similar" in body
