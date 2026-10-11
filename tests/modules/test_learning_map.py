"""Faz 9.1 — the learning map's graph is computed, deterministic and acyclic.

No app, no database: `learning_map_graph` is a pure function over payloads.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from app.modules.scrape.learning_map import (
    MIN_NODES,
    ROLE_CURRENT,
    ROLE_PREREQUISITE,
    learning_map_graph,
)
from app.modules.scrape.sources.payload import PaperPayload


def _work(i, concepts, *, year=2022, cited=0, doi=None, title=None):
    return {
        "title": title or f"Work {i:03d}",
        "categories": list(concepts),
        "published_at": datetime(year, 6, 1, tzinfo=UTC),
        "cited_by_count": cited,
        "doi": doi,
        "url": f"https://example.com/w/{i}",
    }


def _gnn_corpus():
    """A topic ("Graph neural networks", in every work) whose concepts nest:

        Graph theory (40) ⊃ Message passing (24) ⊃ {GCN (10), GAT (10)}

    plus "Drug discovery" (12): mostly inside Graph theory, unrelated to the
    rest. Each inner concept is wholly inside its parent, so P(parent|child)=1
    and P(child|parent)<1 -- the textbook subsumption case.
    """
    works = []
    i = 0

    def add(n, concepts, **kw):
        nonlocal i
        for _ in range(n):
            works.append(_work(i, ["Graph neural networks", *concepts], **kw))
            i += 1

    add(10, ["Graph theory", "Message passing", "GCN"], year=2018)
    add(10, ["Graph theory", "Message passing", "GAT"], year=2019)
    add(4, ["Graph theory", "Message passing"], year=2017)
    add(10, ["Graph theory", "Drug discovery"], year=2024)
    add(6, ["Graph theory"], year=2015)
    add(2, ["Drug discovery"], year=2025)
    # Works whose concepts are each too rare to be a node. Without them Graph
    # theory would sit in 40 of 42 works -- over ROOT_SHARE, i.e. the topic
    # itself rather than a step on the way.
    for k in range(10):
        add(1, [f"Rare concept {k}"], year=2020)
    return works


def _edges(graph):
    return {(e["from"], e["to"]) for e in graph["edges"]}


def _node(graph, name):
    return next(n for n in graph["nodes"] if n["name"] == name)


# --- arrows ------------------------------------------------------------------


def test_nested_concepts_become_a_prerequisite_chain():
    graph = learning_map_graph(_gnn_corpus())
    assert not graph["insufficient"]
    assert _edges(graph) == {
        ("Graph theory", "Message passing"),
        ("Message passing", "GCN"),
        ("Message passing", "GAT"),
        ("Graph theory", "Drug discovery"),
    }


def test_transitive_shortcuts_are_removed():
    # Graph theory subsumes GCN directly too (P=1), but the path through
    # Message passing already says so; the map keeps the path.
    graph = learning_map_graph(_gnn_corpus())
    assert ("Graph theory", "GCN") not in _edges(graph)
    assert ("Graph theory", "GAT") not in _edges(graph)


def test_the_query_topic_titles_the_map_instead_of_being_a_node():
    graph = learning_map_graph(_gnn_corpus())
    assert graph["root"] == "Graph neural networks"
    assert "Graph neural networks" not in {n["name"] for n in graph["nodes"]}


def test_mutual_containment_draws_no_arrow():
    # A and B each carry 9 of the other's 10 works: P(A|B) = P(B|A) = 0.9.
    # Neither is more general; an arrow either way would be invented.
    works = [_work(i, ["A", "B", "X"]) for i in range(9)]
    works += [_work(9, ["A", "X"]), _work(10, ["B", "X"])]
    works += [_work(11 + i, ["C", "X"]) for i in range(10)]
    works += [_work(21 + i, ["D", "X"]) for i in range(10)]
    graph = learning_map_graph(works, min_support=5)
    assert not {("A", "B"), ("B", "A")} & _edges(graph)


def test_identical_concepts_draw_no_arrow():
    # P(B|A) = 1 both ways: the same set of works under two names.
    works = [_work(i, ["A", "B", "X"]) for i in range(10)]
    works += [_work(10 + i, ["C"]) for i in range(10)] + [_work(20 + i, ["D"]) for i in range(10)]
    graph = learning_map_graph(works, min_support=5)
    assert not {("A", "B"), ("B", "A")} & _edges(graph)


def test_below_threshold_draws_no_arrow():
    # B is 7/10 inside A: P(A|B)=0.7 < 0.8.
    works = [_work(i, ["A", "B"]) for i in range(7)]
    works += [_work(7 + i, ["B"]) for i in range(3)]
    works += [_work(10 + i, ["A"]) for i in range(13)]
    works += [_work(30 + i, ["C"]) for i in range(8)] + [_work(40 + i, ["D"]) for i in range(8)]
    assert ("A", "B") not in _edges(learning_map_graph(works))
    assert ("A", "B") in _edges(learning_map_graph(works, threshold=0.7))


def test_edges_carry_the_probability_that_justifies_them():
    graph = learning_map_graph(_gnn_corpus())
    by_pair = {(e["from"], e["to"]): e["p_a_given_b"] for e in graph["edges"]}
    assert by_pair[("Message passing", "GCN")] == 1.0
    # Drug discovery: 10 of its 12 works carry Graph theory.
    assert by_pair[("Graph theory", "Drug discovery")] == pytest.approx(10 / 12, abs=1e-4)


@pytest.mark.parametrize("seed", range(25))
def test_random_corpora_are_acyclic_and_point_from_frequent_to_rare(seed):
    rng = random.Random(seed)
    concepts = [f"C{k}" for k in range(9)]
    works = [
        _work(i, rng.sample(concepts, rng.randint(1, 4)), year=rng.randint(2015, 2025))
        for i in range(rng.randint(40, 160))
    ]
    graph = learning_map_graph(works, min_support=3, threshold=rng.choice([0.5, 0.7, 0.8]))
    count = {n["name"]: n["count"] for n in graph["nodes"]}
    for a, b in _edges(graph):
        assert count[a] > count[b], (a, b)
    # Layers strictly increase along every edge -- a cycle could not satisfy it.
    layer = {n["name"]: n["layer"] for n in graph["nodes"]}
    for a, b in _edges(graph):
        assert layer[b] > layer[a]


# --- nodes, order, roles -----------------------------------------------------


def test_layers_and_reading_order_follow_the_arrows():
    graph = learning_map_graph(_gnn_corpus())
    assert _node(graph, "Graph theory")["layer"] == 0
    assert _node(graph, "Message passing")["layer"] == 1
    assert _node(graph, "GCN")["layer"] == 2
    order = [n["name"] for n in sorted(graph["nodes"], key=lambda n: n["order"])]
    assert order.index("Graph theory") < order.index("Message passing") < order.index("GCN")


def test_parentless_concept_that_subsumes_others_is_the_prerequisite():
    graph = learning_map_graph(_gnn_corpus())
    # Graph theory spans every year (median 2018.5, not the oldest), but it
    # subsumes Message passing and Drug discovery: it is what they build on.
    assert _node(graph, "Graph theory")["median_year"] == 2018.5
    assert _node(graph, "Graph theory")["role"] == ROLE_PREREQUISITE
    assert _node(graph, "Drug discovery")["role"] == ROLE_CURRENT


def test_min_support_and_max_nodes_bound_the_map():
    # At 11, only Graph theory (40), Message passing (24) and Drug discovery
    # (12) survive: three nodes, under MIN_NODES, so no map is drawn.
    thin = learning_map_graph(_gnn_corpus(), min_support=11)
    assert thin["insufficient"] is True
    capped = learning_map_graph(_gnn_corpus(), max_nodes=4)
    assert len(capped["nodes"]) == 4
    # GAT and GCN tie at 10; the cap drops the later name.
    assert "GCN" not in {n["name"] for n in capped["nodes"]}


def test_too_few_concepts_is_insufficient_not_a_wrong_map():
    works = [_work(i, ["A", "B"]) for i in range(20)]
    graph = learning_map_graph(works)
    assert graph["insufficient"] is True
    assert graph["nodes"] == [] and graph["edges"] == []
    assert len({"A", "B"}) < MIN_NODES


def test_empty_and_uncategorised_input():
    assert learning_map_graph([])["insufficient"] is True
    graph = learning_map_graph([_work(0, [])])
    assert graph["work_count"] == 0 and graph["insufficient"] is True


# --- key works ---------------------------------------------------------------


def test_key_work_is_the_most_cited_unless_the_user_already_has_one():
    works = _gnn_corpus()
    works.append(
        _work(900, ["Graph theory", "Message passing", "GCN"], cited=5000, doi="10.1234/top")
    )
    works.append(
        _work(901, ["Graph theory", "Message passing", "GCN"], cited=3, doi="10.1234/mine")
    )

    plain = _node(learning_map_graph(works), "GCN")["key_work"]
    assert plain["doi"] == "10.1234/top" and plain["in_library"] is False

    # A DOI in URL form still matches: library DOIs are normalised too.
    mine = _node(learning_map_graph(works, library_dois=["https://doi.org/10.1234/MINE"]), "GCN")
    assert mine["key_work"]["doi"] == "10.1234/mine"
    assert mine["key_work"]["in_library"] is True


# --- determinism and input shapes --------------------------------------------


def test_shuffled_input_gives_the_same_map():
    works = _gnn_corpus()
    for i, w in enumerate(works):
        w["cited_by_count"] = i % 5  # plenty of citation ties
    expected = learning_map_graph(works)
    rng = random.Random(7)
    for _ in range(5):
        shuffled = works[:]
        rng.shuffle(shuffled)
        got = learning_map_graph(shuffled)
        assert got["nodes"] == expected["nodes"]
        assert got["edges"] == expected["edges"]


def test_accepts_paper_payloads_as_works_in_range_returns_them():
    payloads = [
        PaperPayload(
            source="openalex",
            external_id=f"W{w['title'][-3:]}",
            title=w["title"],
            abstract=None,
            authors=[],
            url=w["url"],
            pdf_url=None,
            published_at=w["published_at"],
            categories=w["categories"],
            cited_by_count=w["cited_by_count"],
        )
        for w in _gnn_corpus()
    ]
    assert _edges(learning_map_graph(payloads)) == _edges(learning_map_graph(_gnn_corpus()))


def test_result_is_json_serialisable_for_report_stats():
    import json

    json.dumps(learning_map_graph(_gnn_corpus()))


def test_a_parentless_concept_with_nothing_under_it_is_not_a_prerequisite():
    works = _gnn_corpus() + [_work(500 + i, ["Benchmarks"], year=2016) for i in range(9)]
    graph = learning_map_graph(works)
    benchmarks = _node(graph, "Benchmarks")
    assert benchmarks["layer"] == 0 and benchmarks["role"] != ROLE_PREREQUISITE
