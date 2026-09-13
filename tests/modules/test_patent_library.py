"""Keeping a patent past the window, and the prior-art link (Faz 8 gaps F2/F3).

The load-bearing properties:

- a kept patent becomes one `papers` row even when the same patent already
  arrived from PatentsView (`US11123456`) or EPO (`US11123456B2`) -- patents
  have no DOI, so `upsert_paper` alone cannot see they are the same;
- asking to keep a patent the user once hid un-hides it;
- the rolling purge takes the corpus copy and leaves the library row;
- the prior-art page links to patent search only when that module exists.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.modules.patent import ingest, service
from app.modules.patent.models import PatentDocument
from app.modules.scrape.models import Paper, UserPaper


@pytest.fixture(autouse=True)
def clean(db):
    def wipe():
        PatentDocument.query.delete()
        UserPaper.query.filter(
            UserPaper.paper_id.in_(db.session.query(Paper.id).filter(Paper.kind == "patent"))
        ).delete(synchronize_session=False)
        Paper.query.filter(Paper.kind == "patent").delete()
        db.session.commit()

    wipe()
    yield
    wipe()


@pytest.fixture
def user(auth_client, db):
    from app.core.models.user import User

    _client, uid = auth_client
    return db.session.get(User, uid)


def _doc(db, number="US11123456B2", grant=date(2026, 9, 8)):
    doc = PatentDocument(
        doc_number=number,
        country="US",
        title="Quantised attention",
        abstract="A method.",
        inventors=["Jane Smith"],
        assignees=["Example AI Inc."],
        cpc_codes=["G06N3/08"],
        grant_date=grant,
        ai_source="cpc_core",
    )
    db.session.add(doc)
    db.session.commit()
    return doc


def _existing_paper(db, source, external_id):
    paper = Paper(
        source=source,
        external_id=external_id,
        title="From a nightly scan",
        kind="patent",
        published_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    db.session.add(paper)
    db.session.commit()
    return paper


class TestAddToLibrary:
    def test_creates_a_patent_row_and_links_it(self, db, user):
        doc = _doc(db)
        link, created = service.add_to_library(user, doc)
        assert created
        paper = db.session.get(Paper, link.paper_id)
        assert (paper.kind, paper.source, paper.external_id) == (
            "patent",
            "uspto_bulk",
            doc.doc_number,
        )
        assert paper.authors == ["Jane Smith"]
        assert "assignee:Example AI Inc." in paper.categories
        assert doc.paper_id == paper.id
        assert service.in_library(user, doc)

    def test_a_second_add_links_nothing_new(self, db, user):
        doc = _doc(db)
        service.add_to_library(user, doc)
        _link, created = service.add_to_library(user, doc)
        assert not created
        assert Paper.query.filter(Paper.kind == "patent").count() == 1

    @pytest.mark.parametrize(
        "source,external_id",
        [("patentsview", "US11123456"), ("epo_ops", "US11123456B2")],
    )
    def test_the_same_patent_from_a_scan_is_reused_not_duplicated(
        self, db, user, source, external_id
    ):
        existing = _existing_paper(db, source, external_id)
        doc = _doc(db)
        link, _ = service.add_to_library(user, doc)
        assert link.paper_id == existing.id
        assert Paper.query.filter(Paper.kind == "patent").count() == 1

    def test_a_different_patent_with_a_prefix_number_is_not_reused(self, db, user):
        """`US1112345` must not match `US11123456`: the base number is compared
        whole, not as a prefix."""
        _existing_paper(db, "patentsview", "US1112345")
        doc = _doc(db)
        service.add_to_library(user, doc)
        assert Paper.query.filter(Paper.kind == "patent").count() == 2

    def test_a_hidden_patent_comes_back_when_kept(self, db, user):
        doc = _doc(db)
        link, _ = service.add_to_library(user, doc)
        link.dismissed_at = datetime.now(UTC)
        db.session.commit()
        assert not service.in_library(user, doc)

        link, created = service.add_to_library(user, doc)
        assert created and link.dismissed_at is None
        assert service.in_library(user, doc)

    def test_the_purge_keeps_what_was_kept(self, app, db, user, monkeypatch):
        doc = _doc(db, grant=date(2026, 7, 1))
        link, _ = service.add_to_library(user, doc)
        paper_id, link_id = link.paper_id, link.id
        monkeypatch.setitem(app.config, "PATENT_WINDOW_WEEKS", 3)
        with app.app_context():
            assert ingest.purge_window(date(2026, 9, 12)) == 1
        assert PatentDocument.query.count() == 0
        assert db.session.get(Paper, paper_id) is not None
        assert db.session.get(UserPaper, link_id) is not None


class TestPages:
    def test_detail_offers_add_then_shows_in_library(self, auth_client, db):
        client, _ = auth_client
        doc = _doc(db)
        body = client.get(f"/patents/{doc.doc_number}").get_data(as_text=True)
        assert "Kütüphaneye ekle" in body

        resp = client.post(f"/patents/{doc.doc_number}/library", follow_redirects=True)
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "Kütüphanenize eklendi" in body
        assert "Kütüphanenizde" in body

    def test_unknown_patent_is_404(self, auth_client):
        client, _ = auth_client
        assert client.post("/patents/US0000000B2/library").status_code == 404

    def test_login_is_required(self, client, db):
        doc = _doc(db)
        assert client.post(f"/patents/{doc.doc_number}/library").status_code in (301, 302)


class TestPriorArtLink:
    def test_prior_art_links_to_patent_search(self, auth_client):
        client, _ = auth_client
        body = client.get("/papers/patents").get_data(as_text=True)
        assert "Takip penceresindeki istemlerde ara" in body
        assert "/patents/search" in body

    def test_no_link_when_the_patent_module_is_absent(self, app, auth_client, monkeypatch):
        client, _ = auth_client
        monkeypatch.delitem(app.view_functions, "patent.search")
        body = client.get("/papers/patents").get_data(as_text=True)
        assert "Takip penceresindeki istemlerde ara" not in body
