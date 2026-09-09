"""Tests for citation graph HTTP routes (Faz 5.4 ③)."""

from __future__ import annotations

from app.modules.scrape import citation_service as cs
from app.modules.scrape.models import Paper, UserPaper


def test_detail_mode_graph_renders_template(app, db, auth_client):
    client, uid = auth_client
    with app.app_context():
        paper = Paper(
            source="openalex",
            external_id="W_TEST_ROOT_1",
            title="Root Citation Graph Paper 1",
            doi="10.5555/root.paper.1",
        )
        db.session.add(paper)
        db.session.commit()

        link = UserPaper(user_id=uid, paper_id=paper.id)
        db.session.add(link)
        db.session.commit()
        link_id = link.id

    resp = client.get(f"/papers/{link_id}?mode=graph")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "citation-graph-wrapper" in html
    assert "cg-network-container" in html


def test_citation_graph_json_endpoint(app, db, auth_client, monkeypatch):
    client, uid = auth_client
    with app.app_context():
        paper = Paper(
            source="openalex",
            external_id="W_TEST_ROOT_2",
            title="Root Citation Graph Paper 2",
            doi="10.5555/root.paper.2",
        )
        db.session.add(paper)
        db.session.commit()

        link = UserPaper(user_id=uid, paper_id=paper.id)
        db.session.add(link)
        db.session.commit()
        link_id = link.id

    mock_graph = {
        "center_id": "W_ROOT_2",
        "nodes": [
            {
                "id": "W_ROOT_2",
                "title": "Root Citation Graph Paper 2",
                "is_center": True,
                "type": "center",
                "in_library": True,
                "user_paper_id": link_id,
            },
            {
                "id": "W_REF",
                "title": "A Referenced Work",
                "is_center": False,
                "type": "reference",
                "in_library": False,
                "user_paper_id": None,
            },
        ],
        "edges": [{"source": "W_ROOT_2", "target": "W_REF", "type": "cites"}],
        "stats": {"references_count": 1, "citations_count": 0, "total_nodes": 2},
        "source": "mock",
    }

    monkeypatch.setattr(
        cs,
        "get_citation_graph_for_user",
        lambda paper, user, center_link_id: mock_graph,
    )

    resp = client.get(f"/papers/{link_id}/citation-graph")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "mock"
    assert len(data["nodes"]) == 2
    assert data["nodes"][0]["is_center"] is True
    assert len(data["edges"]) == 1


def test_add_citation_paper_to_library(app, db, auth_client):
    client, uid = auth_client
    with app.app_context():
        paper = Paper(
            source="openalex",
            external_id="W_TEST_ROOT_3",
            title="Root Citation Graph Paper 3",
            doi="10.5555/root.paper.3",
        )
        db.session.add(paper)
        db.session.commit()

        link = UserPaper(user_id=uid, paper_id=paper.id)
        db.session.add(link)
        db.session.commit()
        link_id = link.id

    post_data = {
        "id": "W_NEW_CIT_3",
        "title": "Discovered Graph Paper 3",
        "authors": ["Dr. Graph", "Prof. Node"],
        "year": 2024,
        "doi": "10.7777/discovered.node.3",
        "venue": "Graph Journal",
        "url": "https://example.com/graph_paper_3",
    }

    resp = client.post(
        f"/papers/{link_id}/citation-graph/add",
        json=post_data,
    )
    assert resp.status_code == 200
    res = resp.get_json()
    assert res["status"] == "ok"
    assert res["created"] is True
    assert "user_paper_id" in res

    with app.app_context():
        new_paper = Paper.query.filter_by(doi="10.7777/discovered.node.3").first()
        assert new_paper is not None
        assert new_paper.title == "Discovered Graph Paper 3"
        assert "Graph Journal" in (new_paper.categories or [])

        user_link = UserPaper.query.filter_by(paper_id=new_paper.id).first()
        assert user_link is not None
        assert user_link.matched_keyword == "citation_graph"
