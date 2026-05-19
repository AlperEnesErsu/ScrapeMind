"""Scrape module — unit tests + a stubbed end-to-end through the Celery task.

We never hit the real arXiv API in tests. The adapter is monkey-patched at
the `arxiv_search` symbol to return a fixed payload list.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.models import IdentifierType, Keyword, UserKeyword
from app.modules.scrape.models import Paper, UserPaper
from app.modules.scrape.service import (
    link_user_paper,
    list_user_papers,
    scrape_arxiv_for_user,
    upsert_paper,
)
from app.modules.scrape.sources.arxiv_source import PaperPayload


def _payload(ext_id: str, *, title="A Title", keyword="transformer") -> PaperPayload:
    return PaperPayload(
        source="arxiv",
        external_id=ext_id,
        title=title,
        abstract="abs",
        authors=["A. One", "B. Two"],
        url=f"http://arxiv.org/abs/{ext_id}",
        pdf_url=f"http://arxiv.org/pdf/{ext_id}",
        published_at=datetime(2026, 1, 15, tzinfo=UTC),
        categories=["cs.LG", "stat.ML"],
    )


@pytest.fixture
def clean(db):
    db.session.execute(text("DELETE FROM user_papers"))
    db.session.execute(text("DELETE FROM papers"))
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()
    db.session.add(
        IdentifierType(
            code="email",
            name="Email",
            validation_regex=r"^[^@]+@[^@]+\.[^@]+$",
            verification_method="email_link",
        )
    )
    db.session.commit()
    u = User(
        username="alice",
        email="alice@ex.com",
        full_name="Alice",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(text("DELETE FROM user_papers"))
    db.session.execute(text("DELETE FROM papers"))
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.query(User).delete()
    db.session.commit()


def test_upsert_paper_is_idempotent(db, clean):
    p1 = upsert_paper(_payload("2401.00001"))
    p2 = upsert_paper(_payload("2401.00001"))
    assert p1.id == p2.id
    assert Paper.query.count() == 1


def test_link_user_paper_dedupes(db, clean):
    paper = upsert_paper(_payload("2401.00002"))
    _, c1 = link_user_paper(clean, paper, matched_keyword="x")
    _, c2 = link_user_paper(clean, paper, matched_keyword="x")
    assert c1 is True
    assert c2 is False
    assert UserPaper.query.filter_by(user_id=clean.id).count() == 1


def test_scrape_skips_when_no_keywords(db, clean):
    result = scrape_arxiv_for_user(clean)
    assert result == {"hits": 0, "linked": 0, "reason": "no_keywords"}


def test_scrape_persists_and_links(db, clean, monkeypatch):
    # Give the user one keyword
    kw = Keyword(value="transformer architectures")
    db.session.add(kw)
    db.session.commit()
    db.session.add(UserKeyword(user_id=clean.id, keyword_id=kw.id))
    db.session.commit()

    # Stub arXiv: two results
    def fake_search(query, *, max_results=25):  # noqa: ARG001
        return [
            _payload("2401.10001", title="On transformer architectures for vision"),
            _payload("2401.10002", title="A new optimizer"),
        ]

    monkeypatch.setattr("app.modules.scrape.service.arxiv_search", fake_search)

    result = scrape_arxiv_for_user(clean)
    assert result["hits"] == 2
    assert result["linked"] == 2

    rows = list_user_papers(clean)
    assert len(rows) == 2
    # Title-matched row gets the right keyword; the other falls back to first kw
    matched = {r.paper.external_id: r.matched_keyword for r in rows}
    assert matched["2401.10001"] == "transformer architectures"
    # Second paper title doesn't contain the kw → falls back to first keyword
    assert matched["2401.10002"] == "transformer architectures"


def test_scrape_run_idempotent(db, clean, monkeypatch):
    kw = Keyword(value="rl")
    db.session.add(kw)
    db.session.commit()
    db.session.add(UserKeyword(user_id=clean.id, keyword_id=kw.id))
    db.session.commit()

    def fake_search(query, *, max_results=25):  # noqa: ARG001
        return [_payload("2401.55555", title="An RL paper")]

    monkeypatch.setattr("app.modules.scrape.service.arxiv_search", fake_search)

    r1 = scrape_arxiv_for_user(clean)
    r2 = scrape_arxiv_for_user(clean)
    assert r1["linked"] == 1
    assert r2["linked"] == 0  # no new links second time around
    assert Paper.query.count() == 1
    assert UserPaper.query.count() == 1


def test_celery_task_through_eager(db, clean, monkeypatch):
    """run_for_user task in eager mode -> calls our service end-to-end."""
    from app.tasks.scrape_tasks import run_for_user

    kw = Keyword(value="diffusion")
    db.session.add(kw)
    db.session.commit()
    db.session.add(UserKeyword(user_id=clean.id, keyword_id=kw.id))
    db.session.commit()

    monkeypatch.setattr(
        "app.modules.scrape.service.arxiv_search",
        lambda q, *, max_results=25: [_payload("2401.77777")],
    )
    result = run_for_user.delay(clean.id).get()
    assert result["linked"] == 1


def test_feed_route_requires_login(client):
    r = client.get("/papers/", follow_redirects=False)
    assert r.status_code in (302, 401)
