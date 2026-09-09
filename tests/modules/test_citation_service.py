"""Tests for citation graph service (Faz 5.4 ③)."""

from __future__ import annotations

from app.core.models.user import User
from app.modules.scrape import citation_service as cs
from app.modules.scrape.models import Paper, UserPaper


class _MockResponse:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


def test_to_node_parses_openalex_format():
    item = {
        "id": "https://openalex.org/W123456",
        "title": "A Great Study in AI",
        "publication_year": 2023,
        "cited_by_count": 42,
        "doi": "https://doi.org/10.1000/182",
        "authorships": [
            {"author": {"display_name": "Alice Smith"}},
            {"author": {"display_name": "Bob Jones"}},
        ],
        "primary_location": {
            "source": {"display_name": "Journal of AI Research"},
            "landing_page_url": "https://example.com/p1",
        },
    }

    node = cs._to_node(item, "reference")
    assert node["id"] == "W123456"
    assert node["title"] == "A Great Study in AI"
    assert node["year"] == 2023
    assert node["citation_count"] == 42
    assert node["doi"] == "10.1000/182"
    assert node["authors"] == ["Alice Smith", "Bob Jones"]
    assert node["venue"] == "Journal of AI Research"
    assert node["type"] == "reference"
    assert node["is_center"] is False
    assert node["in_library"] is False


def test_resolve_openalex_work_by_doi_and_title(monkeypatch):
    monkeypatch.setattr(cs, "openalex_slot", lambda: True)

    paper_with_doi = Paper(
        source="arxiv",
        external_id="2301.0001",
        title="Paper Title",
        doi="10.1000/182",
    )

    def mock_get(url, *args, **kwargs):
        if "10.1000/182" in url:
            return _MockResponse({"id": "https://openalex.org/W999", "title": "Paper Title"})
        return _MockResponse({}, status_code=404)

    monkeypatch.setattr(cs.requests, "get", mock_get)
    res = cs._resolve_openalex_work(paper_with_doi)
    assert res is not None
    assert res["id"] == "https://openalex.org/W999"


def test_fetch_references_and_citations(monkeypatch):
    monkeypatch.setattr(cs, "openalex_slot", lambda: True)

    center_work = {
        "id": "https://openalex.org/W_CENTER",
        "title": "Center Paper",
        "referenced_works": ["https://openalex.org/W_REF1", "https://openalex.org/W_REF2"],
    }

    def mock_get(url, params=None, *args, **kwargs):
        params = params or {}
        filter_str = params.get("filter", "")
        if "openalex_id" in filter_str:
            return _MockResponse(
                {
                    "results": [
                        {"id": "https://openalex.org/W_REF1", "title": "Ref One"},
                        {"id": "https://openalex.org/W_REF2", "title": "Ref Two"},
                    ]
                }
            )
        if "cites:W_CENTER" in filter_str:
            return _MockResponse(
                {
                    "results": [
                        {
                            "id": "https://openalex.org/W_CIT1",
                            "title": "Citing One",
                            "cited_by_count": 10,
                        },
                    ]
                }
            )
        return _MockResponse({"results": []})

    monkeypatch.setattr(cs.requests, "get", mock_get)

    refs = cs._fetch_openalex_references(center_work, max_count=5)
    assert len(refs) == 2
    assert refs[0]["title"] == "Ref One"

    cits = cs._fetch_openalex_citations(center_work, max_count=5)
    assert len(cits) == 1
    assert cits[0]["title"] == "Citing One"


def test_get_raw_citation_graph_openalex(monkeypatch):
    monkeypatch.setattr(cs, "get_json", lambda k: None)
    cached_payload = {}
    monkeypatch.setattr(cs, "set_json", lambda k, v, ttl=None: cached_payload.update(v))

    center_work = {
        "id": "https://openalex.org/W_CENTER",
        "title": "Center Paper Title",
        "publication_year": 2024,
        "referenced_works": ["https://openalex.org/W_REF1"],
    }

    monkeypatch.setattr(cs, "_resolve_openalex_work", lambda paper: center_work)
    monkeypatch.setattr(
        cs,
        "_fetch_openalex_references",
        lambda work, max_count=15: [
            {"id": "W_REF1", "title": "Reference Paper", "type": "reference"}
        ],
    )
    monkeypatch.setattr(
        cs,
        "_fetch_openalex_citations",
        lambda work, max_count=15: [{"id": "W_CIT1", "title": "Citing Paper", "type": "citation"}],
    )

    paper = Paper(
        id=101,
        source="arxiv",
        external_id="2401.0001",
        title="Center Paper Title",
    )

    graph = cs.get_raw_citation_graph(paper)
    assert graph["source"] == "openalex"
    assert len(graph["nodes"]) == 3
    assert len(graph["edges"]) == 2
    assert graph["stats"]["references_count"] == 1
    assert graph["stats"]["citations_count"] == 1
    assert cached_payload.get("source") == "openalex"


def test_get_raw_citation_graph_s2_fallback(monkeypatch):
    monkeypatch.setattr(cs, "get_json", lambda k: None)
    monkeypatch.setattr(cs, "set_json", lambda k, v, ttl=None: None)
    monkeypatch.setattr(cs, "_resolve_openalex_work", lambda paper: None)

    s2_data = {
        "title": "S2 Center Paper",
        "year": 2022,
        "references": [{"title": "S2 Ref 1", "year": 2020}],
        "citations": [{"title": "S2 Cit 1", "year": 2023, "citationCount": 5}],
    }

    def mock_get(url, *args, **kwargs):
        return _MockResponse(s2_data)

    monkeypatch.setattr(cs.requests, "get", mock_get)

    paper = Paper(
        id=102,
        source="arxiv",
        external_id="2201.0001",
        title="S2 Center Paper",
        doi="10.1000/s2paper",
    )

    graph = cs.get_raw_citation_graph(paper)
    assert graph["source"] == "semantic_scholar"
    assert len(graph["nodes"]) == 3
    assert len(graph["edges"]) == 2


def test_get_raw_citation_graph_local_fallback(monkeypatch):
    monkeypatch.setattr(cs, "get_json", lambda k: None)
    monkeypatch.setattr(cs, "set_json", lambda k, v, ttl=None: None)
    monkeypatch.setattr(cs, "_resolve_openalex_work", lambda paper: None)
    monkeypatch.setattr(cs, "_fetch_semantic_scholar_graph", lambda *args, **kwargs: None)

    paper = Paper(
        id=103,
        source="custom",
        external_id="cust1",
        title="Isolated Paper",
    )

    graph = cs.get_raw_citation_graph(paper)
    assert graph["source"] == "local_fallback"
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["is_center"] is True
    assert graph["edges"] == []


def test_decorate_with_user_library(app, db):
    with app.app_context():
        user = User.query.filter_by(username="citation_tester").first()
        if not user:
            from app.core.auth.strategies.local import LocalAuthStrategy

            user = User(
                username="citation_tester",
                email="cit@test.local",
                full_name="Citation Tester",
                password_hash=LocalAuthStrategy.hash_password("Password123!"),
                is_active=True,
            )
            db.session.add(user)
            db.session.commit()

        # Clean existing user papers
        UserPaper.query.filter_by(user_id=user.id).delete()
        db.session.commit()

        p_saved = Paper(
            source="openalex",
            external_id="saved_doi_paper",
            title="Saved in Library Paper",
            doi="10.1234/already.saved",
        )
        db.session.add(p_saved)
        db.session.commit()

        up_saved = UserPaper(user_id=user.id, paper_id=p_saved.id)
        db.session.add(up_saved)
        db.session.commit()

        graph_data = {
            "center_id": "center_1",
            "nodes": [
                {
                    "id": "center_1",
                    "title": "Center Work",
                    "is_center": True,
                    "in_library": False,
                    "user_paper_id": None,
                },
                {
                    "id": "ref_saved",
                    "title": "Saved in Library Paper",
                    "doi": "10.1234/already.saved",
                    "is_center": False,
                    "in_library": False,
                    "user_paper_id": None,
                },
                {
                    "id": "ref_external",
                    "title": "Unsaved External Paper",
                    "doi": "10.9999/not.in.lib",
                    "is_center": False,
                    "in_library": False,
                    "user_paper_id": None,
                },
            ],
            "edges": [],
        }

        decorated = cs.decorate_with_user_library(graph_data, user, center_link_id=9999)

        nodes = {n["id"]: n for n in decorated["nodes"]}
        assert nodes["center_1"]["in_library"] is True
        assert nodes["center_1"]["user_paper_id"] == 9999

        assert nodes["ref_saved"]["in_library"] is True
        assert nodes["ref_saved"]["user_paper_id"] == up_saved.id

        assert nodes["ref_external"]["in_library"] is False
        assert nodes["ref_external"]["user_paper_id"] is None
