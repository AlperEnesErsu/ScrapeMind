"""Patent tracking pages + claim tree (Faz 8.2).

The load-bearing properties here are that the pages render on an **empty**
corpus — which is the normal state until the weekly load ships in 8.3 — and
that the menu entry resolves, because a nav row whose endpoint does not exist
turns every page in the app into a BuildError.
"""

from __future__ import annotations

from datetime import date

import pytest
from flask import url_for

from app.modules.patent import service
from app.modules.patent.models import PatentClaim, PatentDocument


def _doc(
    db, doc_number="US11123456B2", grant_date=date(2026, 9, 8), title="Training a neural network"
):
    document = PatentDocument(
        doc_number=doc_number,
        country="US",
        kind_code="B2",
        title=title,
        abstract="A method of training a neural network.",
        description="Machine learning systems require large datasets.",
        grant_date=grant_date,
        filing_date=date(2023, 2, 14),
        assignees=["Example Robotics Inc."],
        inventors=["Jane Smith"],
        cpc_codes=["G06N3/08"],
        ai_source="cpc_core",
        claim_count=0,
    )
    db.session.add(document)
    db.session.flush()
    return document


def _claim(db, document, number, text="A method comprising receiving data.", depends_on=None):
    claim = PatentClaim(
        patent_document_id=document.id,
        number=number,
        is_independent=depends_on is None,
        depends_on=depends_on,
        text=text,
    )
    db.session.add(claim)
    db.session.flush()
    return claim


class TestIndexPage:
    def test_empty_corpus_says_so_instead_of_rendering_a_blank_page(self, auth_client, db):
        client, _ = auth_client
        PatentDocument.query.delete()
        db.session.commit()
        resp = client.get("/patents/")
        assert resp.status_code == 200
        # Asserted in Turkish: the app renders in its default locale, so this
        # also proves the catalog entry landed rather than falling back.
        assert "Henüz patent yüklenmedi." in resp.get_data(as_text=True)

    def test_documents_are_listed_newest_first(self, auth_client, db):
        client, _ = auth_client
        PatentDocument.query.delete()
        db.session.commit()
        _doc(db, doc_number="US1AAAA1B2", grant_date=date(2026, 9, 1), title="Older patent")
        _doc(db, doc_number="US2BBBB2B2", grant_date=date(2026, 9, 8), title="Newer patent")
        db.session.commit()

        body = client.get("/patents/").get_data(as_text=True)
        assert body.index("Newer patent") < body.index("Older patent")

    def test_login_is_required(self, client):
        resp = client.get("/patents/")
        assert resp.status_code in (301, 302)


class TestDetailPage:
    def test_unknown_document_is_404(self, auth_client):
        client, _ = auth_client
        assert client.get("/patents/US0000000B2").status_code == 404

    def test_claim_one_and_the_tree_are_rendered(self, auth_client, db):
        client, _ = auth_client
        PatentDocument.query.delete()
        db.session.commit()
        document = _doc(db)
        _claim(db, document, 1, "A method comprising receiving training data.")
        _claim(db, document, 2, "The method wherein the data is images.", depends_on=1)
        db.session.commit()

        body = client.get(f"/patents/{document.doc_number}").get_data(as_text=True)
        assert "bu patentin kapsamını tanımlar" in body
        assert "receiving training data" in body
        assert "wherein the data is images" in body
        # The legal boundary that SCRAPING.md section 11 requires to stay.
        assert "hukuki tavsiye değildir" in body


class TestClaimTree:
    def test_dependent_nests_under_its_parent(self, db):
        document = _doc(db, doc_number="US3CCCC3B2")
        _claim(db, document, 1)
        _claim(db, document, 2, depends_on=1)
        _claim(db, document, 3, depends_on=2)
        db.session.commit()

        roots = service.claim_tree(document)
        assert [n.claim.number for n in roots] == [1]
        assert [n.claim.number for n in roots[0].children] == [2]
        assert [n.claim.number for n in roots[0].children[0].children] == [3]

    def test_two_independent_claims_are_both_roots(self, db):
        document = _doc(db, doc_number="US4DDDD4B2")
        _claim(db, document, 1)
        _claim(db, document, 2, depends_on=1)
        _claim(db, document, 3)
        db.session.commit()

        roots = service.claim_tree(document)
        assert [n.claim.number for n in roots] == [1, 3]

    def test_dangling_parent_becomes_a_root_rather_than_disappearing(self, db):
        """A claim-ref to a claim not in this document must not swallow it."""
        document = _doc(db, doc_number="US5EEEE5B2")
        _claim(db, document, 1)
        _claim(db, document, 2, depends_on=99)
        db.session.commit()

        roots = service.claim_tree(document)
        assert sorted(n.claim.number for n in roots) == [1, 2]

    def test_a_cycle_degrades_to_roots_instead_of_recursing_forever(self, db):
        """No valid patent has one, but a bad parse can produce it."""
        document = _doc(db, doc_number="US6FFFF6B2")
        _claim(db, document, 1, depends_on=2)
        _claim(db, document, 2, depends_on=1)
        db.session.commit()

        roots = service.claim_tree(document)
        assert len(roots) >= 1
        assert sum(1 for _ in _walk(roots)) == 2


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.children)


class TestWindow:
    @pytest.mark.parametrize("weeks,expected_days", [(3, 21), (1, 7), (8, 56)])
    def test_window_start_follows_config(self, app, weeks, expected_days):
        with app.app_context():
            app.config["PATENT_WINDOW_WEEKS"] = weeks
            start = service.window_start(now=date(2026, 9, 12))
            assert (date(2026, 9, 12) - start).days == expected_days
        app.config["PATENT_WINDOW_WEEKS"] = 3

    def test_a_nonsense_window_falls_back_rather_than_crashing(self, app):
        with app.app_context():
            app.config["PATENT_WINDOW_WEEKS"] = "not a number"
            assert service.window_weeks() == service.DEFAULT_WINDOW_WEEKS
        app.config["PATENT_WINDOW_WEEKS"] = 3


def test_menu_endpoint_resolves(app):
    """The BuildError trap: `_sidebar.html` calls `url_for` unguarded, so a
    menu row pointing at a missing endpoint breaks every page in the app."""
    from app.modules.patent.manifest import MODULE

    with app.test_request_context():
        for entry in MODULE["menu"]:
            assert url_for(entry["endpoint"])
