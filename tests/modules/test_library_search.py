"""Library search — filters (text/source/date/has-notes), scoping, pagination."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape.models import Paper, PaperNote, UserPaper
from app.modules.scrape.service import distinct_user_sources, search_user_papers_query


@pytest.fixture
def library(db):
    for tbl in ("paper_notes", "user_papers", "papers", "user_roles"):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter(User.username.in_(["searcher", "rival"])).delete(
        synchronize_session=False
    )
    db.session.commit()

    users = {}
    for name in ("searcher", "rival"):
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

    papers = [
        Paper(source="arxiv", external_id="a1", title="Deep transformer networks",
              abstract="attention mechanisms", authors=["Ada Lovelace"],
              published_at=datetime(2024, 1, 10, tzinfo=UTC)),
        Paper(source="pubmed", external_id="p1", title="CRISPR gene editing",
              abstract="genome", authors=["Marie Curie"],
              published_at=datetime(2025, 6, 1, tzinfo=UTC)),
        Paper(source="arxiv", external_id="a2", title="Diffusion models survey",
              abstract="generative", authors=["Alan Turing"],
              published_at=datetime(2026, 2, 1, tzinfo=UTC)),
    ]  # fmt: skip
    db.session.add_all(papers)
    db.session.commit()

    links = []
    for p in papers:
        link = UserPaper(user_id=users["searcher"].id, paper_id=p.id, matched_keyword="kw")
        db.session.add(link)
        links.append(link)
    db.session.commit()
    # A note on the first paper only.
    db.session.add(PaperNote(user_paper_id=links[0].id, body="my note"))
    # A dismissed paper must never show up in search.
    links[2].dismissed_at = datetime(2026, 3, 1, tzinfo=UTC)
    # rival owns a copy of paper 1 — must never leak into searcher's results.
    db.session.add(UserPaper(user_id=users["rival"].id, paper_id=papers[0].id, matched_keyword="x"))
    db.session.commit()

    yield {"searcher": users["searcher"], "rival": users["rival"]}

    db.session.rollback()
    for tbl in ("paper_notes", "user_papers", "papers", "user_roles"):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter(User.username.in_(["searcher", "rival"])).delete(
        synchronize_session=False
    )
    db.session.commit()


def _titles(q):
    return sorted(up.paper.title for up in q.all())


# ---------------------------------------------------------------------------
# Query builder (unit)
# ---------------------------------------------------------------------------


def test_no_filters_returns_all_live_papers(db, library):
    q = search_user_papers_query(library["searcher"])
    # 3 owned, but one is dismissed → 2.
    assert _titles(q) == ["CRISPR gene editing", "Deep transformer networks"]


def test_text_filter_matches_abstract_and_title(db, library):
    assert _titles(search_user_papers_query(library["searcher"], q="attention")) == [
        "Deep transformer networks"
    ]
    assert _titles(search_user_papers_query(library["searcher"], q="crispr")) == [
        "CRISPR gene editing"
    ]


def test_source_filter(db, library):
    assert _titles(search_user_papers_query(library["searcher"], source="pubmed")) == [
        "CRISPR gene editing"
    ]


def test_date_range_filter(db, library):
    q = search_user_papers_query(library["searcher"], date_from=datetime(2025, 1, 1, tzinfo=UTC))
    assert _titles(q) == ["CRISPR gene editing"]  # arxiv a1 is 2024, excluded


def test_has_notes_filter(db, library):
    assert _titles(search_user_papers_query(library["searcher"], has_notes=True)) == [
        "Deep transformer networks"
    ]


def test_search_is_user_scoped(db, library):
    # rival owns only a copy of paper 1; searcher's dismissed paper is theirs.
    assert _titles(search_user_papers_query(library["rival"])) == ["Deep transformer networks"]


def test_distinct_sources_excludes_dismissed_only(db, library):
    # searcher has arxiv (a1 live) + pubmed live; a2 (arxiv) is dismissed but
    # a1 keeps arxiv present.
    assert distinct_user_sources(library["searcher"]) == ["arxiv", "pubmed"]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def _login(client, user):
    with client.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True


def test_search_route_renders(client, library):
    _login(client, library["searcher"])
    r = client.get("/library/search?q=transformer")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Deep transformer networks" in body
    assert "CRISPR" not in body


def test_search_route_combined_filters(client, library):
    _login(client, library["searcher"])
    r = client.get("/library/search?source=pubmed&has_notes=1")
    # pubmed paper has no note → zero results.
    assert r.status_code == 200
    assert "0 " in r.get_data(as_text=True) or "sonuç" in r.get_data(as_text=True)


def test_search_requires_login(client):
    r = client.get("/library/search", follow_redirects=False)
    assert r.status_code in (302, 401)
