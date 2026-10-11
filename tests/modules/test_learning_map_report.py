"""Faz 9.2 — the "learning_map" report kind, end to end.

What is pinned here:

- `create_report` treats "learning_map" like "topic" (keywords, year span,
  the daily cap shared across kinds) and titles it from the query;
- `run_report` spends only the works harvest (no aggregates), stores the
  graph under `stats["graph"]` and the narration under
  `sections["narration"]`, and degrades to `partial` -- never to a lost map --
  when the harvest is throttled, the graph is insufficient, AI is off or the
  narration fails;
- the narration runs in the *owner's* locale, not the worker's;
- the page prints the map as an ordered list (its accessible form,
  docs/PHASE9.md §6) and says why when there is no map.

No network and no LLM: `openalex_source.works_in_range` and
`ai_service.narrate_learning_map` are replaced throughout, and
`aggregate_works` is made to fail loudly so a stray stats call shows up.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from flask_babel import get_locale
from sqlalchemy import text
from werkzeug.security import generate_password_hash

import app.tasks.report_tasks as report_tasks_module
from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape import ai_service, report_service
from app.modules.scrape.learning_map import (
    learning_map_canvas,
    learning_map_graph,
    learning_map_steps,
)
from app.modules.scrape.models import Paper, Report, UserPaper
from app.modules.scrape.ratelimit import SourceThrottledError
from app.modules.scrape.sources import openalex_source as oa
from app.modules.scrape.sources.payload import PaperPayload

KEY_DOI = "10.1234/graph-theory-0"


def _payloads() -> list[PaperPayload]:
    """Four concepts, ten works each, citing along Graph theory -> Message
    passing -> GCN and Graph theory -> Drug discovery. Work 0 of Graph
    theory has a DOI and the most citations, so it is that node's key work."""
    chain = (
        ("Graph theory", None),
        ("Message passing", "Graph theory"),
        ("GCN", "Message passing"),
        ("Drug discovery", "Graph theory"),
    )
    out = []
    for concept, cites in chain:
        slug = concept.replace(" ", "")
        for i in range(10):
            out.append(
                PaperPayload(
                    source="openalex",
                    external_id=f"W{slug}{i}",
                    title=f"{concept} paper {i}",
                    abstract=None,
                    authors=["A. Author"],
                    url=f"https://example.com/{slug}/{i}",
                    pdf_url=None,
                    published_at=datetime(2020 + i % 5, 6, 1, tzinfo=UTC),
                    categories=[concept],
                    doi=KEY_DOI if (concept == "Graph theory" and i == 0) else None,
                    cited_by_count=1000 if i == 0 else i,
                    referenced_works=(
                        (f"W{cites.replace(' ', '')}{i}",) if cites is not None else ()
                    ),
                )
            )
    return out


def _narration(graph, *, label_prefix="Plain "):
    return {
        "nodes": {
            n["id"]: {
                "label": f"{label_prefix}{n['id']}",
                "explain": f"What {n['id']} is about.",
                "check_question": f"Can you explain {n['id']}?",
            }
            for n in graph["nodes"]
        }
    }


#: Child tables before parents, as tests/modules/test_reports.py clears them.
_TABLES = ("notifications", "reports", "paper_notes", "user_papers", "papers")


@pytest.fixture
def owner(db):
    for tbl in _TABLES + ("user_roles",):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="lmowner").delete()
    db.session.commit()
    user = User(
        username="lmowner",
        email="lmowner@example.test",
        full_name="Map Owner",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
        locale="tr",
    )
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.rollback()
    for tbl in _TABLES:
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(id=user.id).delete()
    db.session.commit()


@pytest.fixture
def calls(monkeypatch):
    """Default stubs: the corpus above, AI on, a narration that records the
    locale it ran in. A test overrides only what it is about."""
    recorded: dict = {"narrate": [], "works": []}

    def fake_works(query, *, since_year, until_year, max_results):
        recorded["works"].append((query, since_year, until_year))
        return _payloads()

    def no_aggregates(*a, **kw):  # pragma: no cover - must not run
        raise AssertionError("a learning map must not spend aggregate calls")

    def fake_narrate(graph, *, topic, user=None):
        recorded["narrate"].append({"topic": topic, "locale": str(get_locale()), "user": user})
        return _narration(graph)

    monkeypatch.setattr(oa, "works_in_range", fake_works)
    monkeypatch.setattr(oa, "aggregate_works", no_aggregates)
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "narrate_learning_map", fake_narrate)
    return recorded


def _create(user, keywords=("graph neural networks",), years=5):
    report, error = report_service.create_report(
        user, "learning_map", {"keywords": list(keywords), "years": years}
    )
    assert error is None, error
    return report


# ----------------------------------------------------------------------------
# create_report
# ----------------------------------------------------------------------------


def test_create_report_accepts_learning_map_and_titles_it_from_the_query(app, db, owner):
    report = _create(owner, keywords=("graph neural networks", "GNN"), years=3)

    assert report.kind == "learning_map"
    assert report.status == "pending"
    assert report.params == {"keywords": ["graph neural networks", "GNN"], "years": 3}
    assert "graph neural networks, GNN" in report.title
    assert "learning_map" in report_service.VALID_KINDS
    assert "learning_map" in report_service._RUNNABLE_KINDS


def test_create_report_validates_learning_map_like_topic(app, db, owner):
    row, error = report_service.create_report(owner, "learning_map", {"keywords": [], "years": 3})
    assert row is None
    assert str(error) == str(report_service.REPORT_KEYWORDS_REQUIRED_MESSAGE)

    for years in (0, 99, "x", None):
        row, error = report_service.create_report(
            owner, "learning_map", {"keywords": ["gnn"], "years": years}
        )
        assert row is None and error


def test_daily_cap_is_shared_with_topic_reports(app, db, owner, monkeypatch):
    monkeypatch.setattr(report_service, "MAX_REPORTS_PER_DAY", 2)
    report_service.create_report(owner, "topic", {"keywords": ["a"], "years": 1})
    report_service.create_report(owner, "learning_map", {"keywords": ["b"], "years": 1})

    row, error = report_service.create_report(
        owner, "learning_map", {"keywords": ["c"], "years": 1}
    )

    assert row is None
    assert str(error) == str(report_service.REPORT_DAILY_CAP_MESSAGE)


# ----------------------------------------------------------------------------
# run_report
# ----------------------------------------------------------------------------


def test_happy_path_stores_graph_and_narration(app, db, owner, calls):
    report = _create(owner)

    result = report_service.run_report(report)

    assert result == {"status": "ok", "item_count": 40}
    assert report.status == "ok"
    assert report.item_count == 40
    assert set(report.stats) == {"graph", "query", "since_year", "until_year"}
    assert report.stats["query"] == "graph neural networks"
    assert report.stats["until_year"] - report.stats["since_year"] == 4
    graph = report.stats["graph"]
    assert graph["insufficient"] is False
    assert {n["id"] for n in graph["nodes"]} == {
        "Graph theory",
        "Message passing",
        "GCN",
        "Drug discovery",
    }
    assert {(e["from"], e["to"]) for e in graph["edges"]} == {
        ("Graph theory", "Message passing"),
        ("Message passing", "GCN"),
        ("Graph theory", "Drug discovery"),
    }
    assert report.sections == {"narration": _narration(graph)}
    assert calls["narrate"][0]["topic"] == "graph neural networks"
    assert calls["narrate"][0]["user"] is owner
    # The works are upserted like a topic report's, so key works resolve to rows.
    assert Paper.query.count() == 40
    assert UserPaper.query.count() == 0


def test_library_dois_mark_the_key_work_in_library(app, db, owner, calls):
    paper = Paper(source="crossref", external_id="lib-1", title="Mine", doi=KEY_DOI)
    db.session.add(paper)
    db.session.flush()
    db.session.add(UserPaper(user_id=owner.id, paper_id=paper.id))
    db.session.commit()
    report = _create(owner)

    report_service.run_report(report)

    nodes = {n["id"]: n for n in report.stats["graph"]["nodes"]}
    assert nodes["Graph theory"]["key_work"]["doi"] == KEY_DOI
    assert nodes["Graph theory"]["key_work"]["in_library"] is True
    assert nodes["GCN"]["key_work"]["in_library"] is False


def test_a_dismissed_library_paper_does_not_count(app, db, owner, calls):
    paper = Paper(source="crossref", external_id="lib-2", title="Hidden", doi=KEY_DOI)
    db.session.add(paper)
    db.session.flush()
    db.session.add(UserPaper(user_id=owner.id, paper_id=paper.id, dismissed_at=datetime.now(UTC)))
    db.session.commit()
    report = _create(owner)

    report_service.run_report(report)

    nodes = {n["id"]: n for n in report.stats["graph"]["nodes"]}
    assert nodes["Graph theory"]["key_work"]["in_library"] is False


def test_another_users_library_does_not_count(app, db, owner, calls):
    other = User(
        username="lmother",
        email="lmother@example.test",
        full_name="Other",
        password_hash=generate_password_hash("x12345678"),
        is_active=True,
    )
    db.session.add(other)
    paper = Paper(source="crossref", external_id="lib-3", title="Theirs", doi=KEY_DOI)
    db.session.add(paper)
    db.session.flush()
    db.session.add(UserPaper(user_id=other.id, paper_id=paper.id))
    db.session.commit()
    try:
        report = _create(owner)
        report_service.run_report(report)
        nodes = {n["id"]: n for n in report.stats["graph"]["nodes"]}
        assert nodes["Graph theory"]["key_work"]["in_library"] is False
    finally:
        db.session.execute(text("DELETE FROM user_papers WHERE user_id = :u"), {"u": other.id})
        db.session.delete(other)
        db.session.commit()


@pytest.mark.parametrize("locale", ["en", "tr"])
def test_narration_runs_in_the_owners_locale(app, db, owner, calls, locale):
    owner.locale = locale
    db.session.commit()
    report = _create(owner)

    report_service.run_report(report)

    assert [c["locale"] for c in calls["narrate"]] == [locale]


def test_insufficient_graph_is_partial_and_not_narrated(app, db, owner, calls, monkeypatch):
    # Every work in one concept: too few concepts for a map.
    flat = [
        PaperPayload(
            source="openalex",
            external_id=f"WF{i}",
            title=f"Flat {i}",
            abstract=None,
            authors=[],
            url=None,
            pdf_url=None,
            published_at=None,
            categories=["Only one"],
        )
        for i in range(12)
    ]
    monkeypatch.setattr(oa, "works_in_range", lambda *a, **kw: flat)
    report = _create(owner)

    report_service.run_report(report)

    assert report.status == "partial"
    assert report.stats["graph"]["insufficient"] is True
    assert report.stats["graph"]["reason"] == "too_few_concepts"
    assert report.sections == {}
    assert report.item_count == 12
    assert calls["narrate"] == []


def test_narration_none_is_partial_but_keeps_the_map(app, db, owner, calls, monkeypatch):
    monkeypatch.setattr(ai_service, "narrate_learning_map", lambda graph, *, topic, user=None: None)
    report = _create(owner)

    report_service.run_report(report)

    assert report.status == "partial"
    assert report.stats["graph"]["insufficient"] is False
    assert report.stats["graph"]["nodes"]
    assert report.sections == {}


def test_ai_disabled_is_partial_without_a_call(app, db, owner, calls, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)
    report = _create(owner)

    report_service.run_report(report)

    assert report.status == "partial"
    assert report.stats["graph"]["nodes"]
    assert report.sections == {}
    assert calls["narrate"] == []


def test_throttled_harvest_is_partial(app, db, owner, calls, monkeypatch):
    def throttled(*a, **kw):
        raise SourceThrottledError("openalex")

    monkeypatch.setattr(oa, "works_in_range", throttled)
    report = _create(owner)

    report_service.run_report(report)

    assert report.status == "partial"
    assert report.item_count == 0
    assert report.stats["graph"]["insufficient"] is True
    assert calls["narrate"] == []


def test_unexpected_exception_is_an_error_and_reraises(app, db, owner, calls, monkeypatch):
    def boom(graph, *, topic, user=None):
        raise ValueError("broken")

    monkeypatch.setattr(ai_service, "narrate_learning_map", boom)
    report = _create(owner)

    with pytest.raises(ValueError):
        report_service.run_report(report)

    assert report.status == "error"
    assert report.error == "ValueError"
    assert report.finished_at is not None


# ----------------------------------------------------------------------------
# learning_map_steps — the list the page prints
# ----------------------------------------------------------------------------


def _graph():
    return learning_map_graph(_payloads(), library_dois=[KEY_DOI])


def test_steps_follow_reading_order_and_write_out_the_arrows():
    graph = _graph()
    steps = learning_map_steps(graph, _narration(graph))

    assert [s["order"] for s in steps] == list(range(len(steps)))
    by_id = {s["id"]: s for s in steps}
    assert by_id["Graph theory"]["parents"] == []
    assert by_id["Message passing"]["parents"] == ["Plain Graph theory"]
    assert by_id["GCN"]["parents"] == ["Plain Message passing"]
    assert by_id["Graph theory"]["label"] == "Plain Graph theory"
    assert by_id["Graph theory"]["name"] == "Graph theory"
    assert by_id["GCN"]["check_question"] == "Can you explain GCN?"


def test_steps_without_narration_fall_back_to_openalex_names():
    steps = learning_map_steps(_graph(), None)

    by_id = {s["id"]: s for s in steps}
    assert by_id["Message passing"]["label"] == "Message passing"
    assert by_id["Message passing"]["parents"] == ["Graph theory"]
    assert by_id["Message passing"]["explain"] == ""


def test_steps_only_link_http_urls_or_dois():
    graph = _graph()
    nodes = graph["nodes"]
    nodes[0]["key_work"] = {"title": "x", "url": "javascript:alert(1)", "doi": None}
    nodes[1]["key_work"] = {"title": "y", "url": "javascript:alert(1)", "doi": "10.5555/abc"}
    nodes[2]["key_work"] = {"title": "z", "url": "https://example.com/z", "doi": "10.5555/z"}

    steps = learning_map_steps(graph)

    assert steps[0]["key_work_href"] is None
    assert steps[1]["key_work_href"] == "https://doi.org/10.5555/abc"
    assert steps[2]["key_work_href"] == "https://example.com/z"


@pytest.mark.parametrize(
    "graph",
    [None, {}, {"insufficient": True, "nodes": [{"id": "a"}]}, {"nodes": "junk", "edges": 3}],
)
def test_steps_tolerate_missing_or_malformed_graphs(graph):
    assert learning_map_steps(graph, {"nodes": "junk"}) == []


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------


def _stub_delay(monkeypatch):
    queued = []
    monkeypatch.setattr(report_tasks_module.generate, "delay", lambda rid: queued.append(rid))
    return queued


def _make_report(user_id, **overrides):
    fields = {
        "user_id": user_id,
        "kind": "learning_map",
        "title": "Learning map: graph neural networks",
        "params": {"keywords": ["graph neural networks"], "years": 5},
        "status": "ok",
        "item_count": 40,
    }
    fields.update(overrides)
    row = Report(**fields)
    from app.extensions import db

    db.session.add(row)
    db.session.commit()
    return row


def _stats(graph):
    return {
        "graph": graph,
        "query": "graph neural networks",
        "since_year": 2022,
        "until_year": 2026,
    }


def test_reports_page_offers_the_learning_map_kind(auth_client):
    client, _uid = auth_client
    body = client.get("/papers/reports?lang=en").get_data(as_text=True)

    assert 'name="kind"' in body
    assert 'value="learning_map"' in body
    assert "<legend" in body
    assert 'aria-describedby="report-kind-help-learning_map"' in body


def test_post_learning_map_queues_generate_and_redirects(auth_client, db, monkeypatch):
    client, uid = auth_client
    queued = _stub_delay(monkeypatch)

    resp = client.post(
        "/papers/reports/new",
        data={"kind": "learning_map", "keywords": "graph neural networks", "years": "5"},
    )

    row = Report.query.filter_by(user_id=uid).one()
    assert row.kind == "learning_map"
    assert queued == [row.id]
    assert resp.status_code in (301, 302)
    assert resp.headers["Location"].endswith(f"/papers/reports/{row.id}")


def test_post_with_an_unknown_kind_is_rejected(auth_client, db, monkeypatch):
    client, uid = auth_client
    queued = _stub_delay(monkeypatch)

    client.post("/papers/reports/new", data={"kind": "author_group", "keywords": "x", "years": "5"})

    assert Report.query.filter_by(user_id=uid).count() == 0
    assert queued == []


def test_detail_renders_the_ordered_list(auth_client, db):
    client, uid = auth_client
    graph = _graph()
    row = _make_report(uid, stats=_stats(graph), sections={"narration": _narration(graph)})

    body = client.get(f"/papers/reports/{row.id}?lang=en").get_data(as_text=True)

    assert 'data-testid="lm-steps"' in body
    assert (
        body.index('id="lm-step-0"') < body.index('id="lm-step-1"') < body.index('id="lm-step-3"')
    )
    assert 'data-node-id="Graph theory"' in body
    assert 'data-order="0"' in body
    assert "Plain Message passing" in body
    assert "OpenAlex topic: Message passing" in body
    assert "Prerequisite" in body  # the role, as text
    assert "lm-role--prerequisite" in body
    assert "Builds on:" in body
    assert "What GCN is about." in body
    assert "Can you explain GCN?" in body
    assert 'href="https://example.com/Graphtheory/0"' in body
    assert 'rel="noopener noreferrer"' in body
    assert 'data-testid="lm-in-library"' in body
    assert "not written by AI" in body
    assert "40 works, " in body
    # The generic "AI summary" notice belongs to the other kinds.
    assert 'data-testid="report-partial-notice"' not in body
    assert "<script" not in body.split('data-testid="lm-steps"')[1].split("</ol>")[0]


def test_detail_without_narration_says_so_and_uses_openalex_names(auth_client, db):
    client, uid = auth_client
    row = _make_report(uid, status="partial", stats=_stats(_graph()), sections={})

    body = client.get(f"/papers/reports/{row.id}?lang=en").get_data(as_text=True)

    assert 'data-testid="lm-no-narration"' in body
    assert "Message passing" in body
    assert "OpenAlex topic:" not in body


@pytest.mark.parametrize(
    "reason, phrase",
    [
        ("too_few_concepts", "too few concepts"),
        ("no_citation_flow", "rarely cite each other"),
    ],
)
def test_detail_explains_an_insufficient_map(auth_client, db, reason, phrase):
    client, uid = auth_client
    graph = {
        "root": None,
        "nodes": [],
        "edges": [],
        "insufficient": True,
        "reason": reason,
        "params": {},
        "work_count": 30,
        "citation_count": 2,
    }
    row = _make_report(uid, status="partial", stats=_stats(graph), sections={})

    body = client.get(f"/papers/reports/{row.id}?lang=en").get_data(as_text=True)

    assert 'data-testid="lm-insufficient"' in body
    assert phrase in body
    assert "longer time range" in body
    assert 'data-testid="lm-steps"' not in body


def test_detail_renders_in_turkish_by_default(auth_client, db):
    client, uid = auth_client
    row = _make_report(uid, stats=_stats(_graph()), sections={})

    body = client.get(f"/papers/reports/{row.id}").get_data(as_text=True)

    assert "Ön koşul" in body
    assert "Okuma sırası" in body


def test_status_poll_works_for_a_learning_map(auth_client, db):
    client, uid = auth_client
    running = _make_report(uid, status="running")
    done = _make_report(uid, status="ok", stats=_stats(_graph()))

    polling = client.get(f"/papers/reports/{running.id}/status")
    finished = client.get(f"/papers/reports/{done.id}/status")

    assert polling.status_code == 200 and "hx-trigger" in polling.get_data(as_text=True)
    assert finished.headers.get("HX-Refresh") == "true"


def test_another_users_learning_map_is_404(auth_client, db):
    client, _uid = auth_client
    other = User.query.filter_by(email="lmforeign@example.test").first()
    if other is None:
        other = User(
            username="lmforeign",
            email="lmforeign@example.test",
            full_name="Foreign",
            password_hash=generate_password_hash("x12345678"),
            is_active=True,
        )
        db.session.add(other)
        db.session.commit()
    foreign = _make_report(other.id, stats=_stats(_graph()))
    try:
        assert client.get(f"/papers/reports/{foreign.id}").status_code == 404
    finally:
        db.session.delete(foreign)
        db.session.delete(other)
        db.session.commit()


# ----------------------------------------------------------------------------
# Faz 9.4 -- the canvas and the step card on top of the list
# ----------------------------------------------------------------------------

STATIC_JS = Path(__file__).resolve().parents[2] / "app" / "modules" / "scrape" / "static" / "js"


def _explorer_graph(body: str) -> dict | None:
    """The `data-lm-graph` attribute as the browser would decode it."""

    class _Finder(HTMLParser):
        found: dict | None = None

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if values.get("id") == "lm-explorer" and "data-lm-graph" in values:
                self.found = json.loads(values["data-lm-graph"])

    finder = _Finder()
    finder.feed(body)
    return finder.found


def test_canvas_data_mirrors_the_steps():
    graph = _graph()
    steps = learning_map_steps(graph, _narration(graph))

    canvas = learning_map_canvas(graph, steps)

    assert [n["id"] for n in canvas["nodes"]] == [s["id"] for s in steps]
    by_id = {n["id"]: n for n in canvas["nodes"]}
    assert by_id["Message passing"]["label"] == "Plain Message passing"
    assert set(by_id["GCN"]) == {"id", "label", "role", "layer", "order"}
    layers = {n["id"]: n["layer"] for n in graph["nodes"]}
    assert all(n["layer"] == layers[n["id"]] for n in canvas["nodes"])
    assert {(e["from"], e["to"]) for e in canvas["edges"]} == {
        (e["from"], e["to"]) for e in graph["edges"]
    }
    assert all(set(e) == {"from", "to", "flow"} for e in canvas["edges"])


def test_canvas_drops_dangling_and_malformed_edges():
    graph = _graph()
    graph["edges"] = [
        *graph["edges"],
        {"from": "Graph theory", "to": "Nowhere", "flow": 9},
        {"from": "GCN", "to": "GCN", "flow": 9},
        "junk",
        {"from": None, "to": "GCN"},
    ]

    canvas = learning_map_canvas(graph, learning_map_steps(graph))

    ids = {n["id"] for n in canvas["nodes"]}
    assert len(canvas["edges"]) == len(graph["edges"]) - 4
    assert all(
        e["from"] in ids and e["to"] in ids and e["from"] != e["to"] for e in canvas["edges"]
    )


def test_no_canvas_without_steps_or_arrows():
    graph = _graph()
    assert learning_map_canvas(graph, []) is None
    assert learning_map_canvas(None, learning_map_steps(graph)) is None
    graph["edges"] = []
    assert learning_map_canvas(graph, learning_map_steps(graph)) is None


def test_detail_carries_the_canvas_and_a_hidden_step_card(auth_client, db):
    client, uid = auth_client
    graph = _graph()
    narration = _narration(graph)
    row = _make_report(uid, stats=_stats(graph), sections={"narration": narration})

    body = client.get(f"/papers/reports/{row.id}?lang=en").get_data(as_text=True)

    # Progressive enhancement: the block ships hidden, the list is intact.
    assert re.search(r'<section[^>]*id="lm-explorer"[^>]*\shidden[\s>]', body)
    assert 'data-testid="lm-steps"' in body
    assert re.search(r'id="lm-canvas"[^>]*aria-hidden="true"', body)
    assert re.search(r'<button type="button"[^>]*id="lm-prev"', body)
    assert re.search(r'<button type="button"[^>]*id="lm-next"', body)
    assert 'data-msg-step="Step {n} of {total}"' in body
    assert 'data-role-prerequisite="Prerequisite"' in body
    expected = learning_map_canvas(graph, learning_map_steps(graph, narration))
    assert _explorer_graph(body) == expected
    assert body.index("js/vis_loader.js") < body.index("js/learning_map.js")


def test_canvas_data_cannot_break_out_of_its_attribute(auth_client, db):
    client, uid = auth_client
    graph = _graph()
    evil = "</script><img src=x onerror=alert(1)> 'single' \"double\" & co"
    narration = _narration(graph)
    narration["nodes"]["GCN"]["label"] = evil
    row = _make_report(uid, stats=_stats(graph), sections={"narration": narration})

    body = client.get(f"/papers/reports/{row.id}?lang=en").get_data(as_text=True)

    attribute = re.search(r"data-lm-graph='([^']*)'", body).group(1)
    assert "<" not in attribute and ">" not in attribute and "& co" not in attribute
    assert "<img src=x" not in body
    labels = {n["id"]: n["label"] for n in _explorer_graph(body)["nodes"]}
    assert labels["GCN"] == evil


def test_detail_without_arrows_keeps_the_card_but_draws_no_canvas(auth_client, db):
    client, uid = auth_client
    graph = _graph()
    graph["edges"] = []
    row = _make_report(uid, stats=_stats(graph), sections={})

    body = client.get(f"/papers/reports/{row.id}?lang=en").get_data(as_text=True)

    assert 'id="lm-explorer"' in body
    assert "data-lm-graph" not in body
    assert 'id="lm-canvas"' not in body
    assert "js/vis_loader.js" not in body
    assert "js/learning_map.js" in body


def test_insufficient_map_has_no_explorer_and_no_scripts(auth_client, db):
    client, uid = auth_client
    graph = {"nodes": [], "edges": [], "insufficient": True, "reason": "too_few_concepts"}
    row = _make_report(uid, status="partial", stats=_stats(graph), sections={})

    body = client.get(f"/papers/reports/{row.id}?lang=en").get_data(as_text=True)

    assert 'id="lm-explorer"' not in body
    assert "js/learning_map.js" not in body
    assert "js/vis_loader.js" not in body


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
@pytest.mark.parametrize("name", ["learning_map.js", "vis_loader.js", "citation_graph.js"])
def test_page_scripts_parse(name):
    """A syntax error in a static script fails nowhere else in this suite."""
    result = subprocess.run(
        ["node", "--check", str(STATIC_JS / name)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_unconnected_concepts_are_named_under_the_list(app):
    """9.5: concepts no arrow touches leave the map but are announced."""
    from flask import render_template

    from app.modules.scrape.learning_map import learning_map_canvas, learning_map_steps

    graph = {
        "insufficient": False,
        "reason": None,
        "work_count": 10,
        "citation_count": 5,
        "unconnected": ["Protein Structure and Dynamics"],
        "nodes": [
            {
                "id": n,
                "name": n,
                "count": 5,
                "layer": i,
                "order": i,
                "role": "core",
                "key_work": None,
            }
            for i, n in enumerate(["A", "B", "C", "D"])
        ],
        "edges": [
            {"from": a, "to": b, "flow": 6, "reverse_flow": 0} for a, b in ("AB", "BC", "CD")
        ],
    }

    class _Report:
        title = "t"
        stats = {"graph": graph, "query": "q"}
        sections = {}
        status = "partial"

    steps = learning_map_steps(graph, None)
    with app.test_request_context():
        html = render_template(
            "reports/_learning_map_report.html",
            report=_Report(),
            stats=_Report.stats,
            graph=graph,
            learning_map_steps=steps,
            learning_map_canvas=learning_map_canvas(graph, steps),
            narrated=False,
        )
    assert 'data-testid="lm-unconnected"' in html
    assert "Protein Structure and Dynamics" in html
