"""Faz 9.1b — the learning map's arrows come from citation flow.

No app, no database: `learning_map_graph` is a pure function over payloads.
A corpus here is built from concept groups (works that carry one concept
besides the topic) and explicit citations between them, so every expected
arrow can be counted by hand.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from itertools import product

import pytest

from app.modules.scrape.learning_map import (
    MIN_NODES,
    REASON_NO_CITATION_FLOW,
    REASON_TOO_FEW_CONCEPTS,
    ROLE_CORE,
    ROLE_CURRENT,
    ROLE_METHOD,
    ROLE_PREREQUISITE,
    learning_map_graph,
)
from app.modules.scrape.sources.payload import PaperPayload

TOPIC = "Graph neural networks"


def _work(i, concepts, *, refs=(), year=2022, cited=0, doi=None, title=None):
    return {
        "external_id": f"W{i}",
        "title": title or f"Work {i:03d}",
        "categories": list(concepts),
        "referenced_works": list(refs),
        "published_at": datetime(year, 6, 1, tzinfo=UTC),
        "cited_by_count": cited,
        "doi": doi,
        "url": f"https://example.com/w/{i}",
    }


class _Corpus:
    """Concept groups plus citations between them."""

    def __init__(self, *, topic=TOPIC):
        self.topic = topic
        self.groups: dict[str, list[dict]] = {}
        self.works: list[dict] = []

    def group(self, name, n, *, year=2022, extra=()):
        start = len(self.works)
        concepts = [c for c in (self.topic, name, *extra) if c]
        made = [_work(start + k, concepts, year=year) for k in range(n)]
        self.groups[name] = made
        self.works.extend(made)
        return self

    def cite(self, citing, cited, k):
        """`k` distinct (citing work, cited work) pairs: `citing` builds on
        `cited`, so the expected arrow is cited -> citing."""
        pairs = list(product(self.groups[citing], self.groups[cited]))
        assert k <= len(pairs)
        for src, dst in pairs[:k]:
            src["referenced_works"].append(dst["external_id"])
        return self


def _gnn():
    """Graph theory -> Message passing -> {GCN, GAT}; Graph theory -> Drug
    discovery. GCN also cites Graph theory directly: a transitive shortcut."""
    return (
        _Corpus()
        .group("Graph theory", 10, year=2015)
        .group("Message passing", 10, year=2017)
        .group("GCN", 10, year=2018)
        .group("GAT", 10, year=2019)
        .group("Drug discovery", 10, year=2024)
        .cite("Message passing", "Graph theory", 10)
        .cite("GCN", "Message passing", 10)
        .cite("GAT", "Message passing", 9)
        .cite("GCN", "Graph theory", 7)
        .cite("Drug discovery", "Graph theory", 8)
    )


def _edges(graph):
    return {(e["from"], e["to"]) for e in graph["edges"]}


def _node(graph, name):
    return next(n for n in graph["nodes"] if n["name"] == name)


def _abcd(cites, *, extra_groups=(), **kw):
    """Four equal concepts A-D (10 works each) and the given citations."""
    corpus = _Corpus()
    for name in ("A", "B", "C", "D", *extra_groups):
        corpus.group(name, 10)
    for citing, cited, k in cites:
        corpus.cite(citing, cited, k)
    return learning_map_graph(corpus.works, **kw)


# --- arrows ------------------------------------------------------------------


def test_citations_become_a_prerequisite_chain():
    graph = learning_map_graph(_gnn().works)
    assert not graph["insufficient"] and graph["reason"] is None
    assert _edges(graph) == {
        ("Graph theory", "Message passing"),
        ("Message passing", "GCN"),
        ("Message passing", "GAT"),
        ("Graph theory", "Drug discovery"),
    }


def test_transitive_shortcuts_are_removed():
    # GCN cites Graph theory 7 times -- enough for an arrow on its own -- but
    # the path through Message passing already says so; the map keeps the path.
    graph = learning_map_graph(_gnn().works)
    assert ("Graph theory", "GCN") not in _edges(graph)


def test_edges_carry_the_counts_that_justify_them():
    corpus = _gnn().cite("Graph theory", "Drug discovery", 3)
    by_pair = {(e["from"], e["to"]): e for e in learning_map_graph(corpus.works)["edges"]}
    assert by_pair[("Graph theory", "Drug discovery")] == {
        "from": "Graph theory",
        "to": "Drug discovery",
        "flow": 8,
        "reverse_flow": 3,
    }
    assert by_pair[("Message passing", "GAT")]["flow"] == 9


def test_mutual_citation_draws_no_arrow():
    # 6 one way, 4 the other: 6 < 2 * 4. Neither builds on the other.
    graph = _abcd([("B", "A", 6), ("A", "B", 4), ("D", "C", 10)])
    assert not graph["insufficient"]
    assert not {("A", "B"), ("B", "A")} & _edges(graph)
    assert ("C", "D") in _edges(graph)


def test_ratio_is_the_bar_for_direction():
    cites = [("B", "A", 6), ("A", "B", 3), ("D", "C", 10)]
    assert ("A", "B") in _edges(_abcd(cites))  # 6 >= 2 * 3
    assert ("A", "B") not in _edges(_abcd(cites, ratio=2.5))


def test_an_even_tie_at_ratio_one_still_draws_one_arrow_not_two():
    # ratio=1 admits both directions; the greedy step keeps the first by
    # name and refuses the second because it would close a 2-cycle.
    graph = _abcd([("B", "A", 6), ("A", "B", 6), ("D", "C", 10)], ratio=1.0)
    assert {("A", "B"), ("B", "A")} & _edges(graph) == {("A", "B")}


def test_min_flow_is_the_bar_for_existence():
    cites = [("B", "A", 4), ("D", "C", 10)]
    assert ("A", "B") not in _edges(_abcd(cites))
    assert ("A", "B") in _edges(_abcd(cites, min_flow=4))


def test_a_cycle_in_the_flow_loses_its_weakest_edge():
    # A -> B (10), B -> C (8), C -> A (6): every pair passes the ratio rule on
    # its own. Strongest first, C -> A is the one that would close the loop.
    graph = _abcd([("B", "A", 10), ("C", "B", 8), ("A", "C", 6)])
    assert _edges(graph) == {("A", "B"), ("B", "C")}
    layer = {n["name"]: n["layer"] for n in graph["nodes"]}
    assert layer["A"] < layer["B"] < layer["C"]


def test_references_outside_the_corpus_are_ignored():
    corpus = _gnn()
    expected = learning_map_graph(corpus.works)
    for w in corpus.works:
        w["referenced_works"] = ["W999999", *w["referenced_works"], "W888888"]
    graph = learning_map_graph(corpus.works)
    assert graph["edges"] == expected["edges"]
    assert graph["citation_count"] == expected["citation_count"] == 44


def test_a_work_citing_itself_counts_for_nothing():
    # Ten works carrying both A and B, each citing itself: counted, that would
    # be flow 10 both ways and (at ratio 1) an arrow.
    corpus = _Corpus()
    for name in ("C", "D", "E"):
        corpus.group(name, 10)
    corpus.group("A", 10, extra=("B",))
    for w in corpus.groups["A"]:
        w["referenced_works"].append(w["external_id"])
    corpus.cite("D", "C", 10)
    graph = learning_map_graph(corpus.works, ratio=1.0)
    assert graph["citation_count"] == 10
    assert not {("A", "B"), ("B", "A")} & _edges(graph)


def test_duplicate_references_in_a_dict_payload_count_once():
    corpus = _Corpus()
    for name in ("A", "B", "C", "D"):
        corpus.group(name, 10)
    for w in corpus.groups["B"][:4]:
        w["referenced_works"] += [corpus.groups["A"][0]["external_id"]] * 3
    corpus.cite("D", "C", 10)
    assert ("A", "B") not in _edges(learning_map_graph(corpus.works))  # 4 < 5


@pytest.mark.parametrize("seed", range(25))
def test_random_corpora_are_acyclic_and_deterministic(seed):
    rng = random.Random(seed)
    concepts = [f"C{k}" for k in range(9)]
    n = rng.randint(40, 160)
    works = [
        _work(
            i,
            rng.sample(concepts, rng.randint(1, 3)),
            refs=[f"W{rng.randrange(n + 20)}" for _ in range(rng.randint(0, 8))],
            year=rng.randint(2015, 2025),
            cited=rng.randint(0, 3),
        )
        for i in range(n)
    ]
    min_flow, ratio = rng.randint(1, 3), rng.choice([1.0, 1.5, 2.0])
    graph = learning_map_graph(works, min_support=3, min_flow=min_flow, ratio=ratio)

    # Layers strictly increase along every edge -- a cycle could not satisfy it.
    layer = {n["name"]: n["layer"] for n in graph["nodes"]}
    for e in graph["edges"]:
        assert layer[e["to"]] > layer[e["from"]], e
        assert e["flow"] >= min_flow and e["flow"] >= ratio * e["reverse_flow"]

    for _ in range(3):
        shuffled = works[:]
        rng.shuffle(shuffled)
        assert learning_map_graph(shuffled, min_support=3, min_flow=min_flow, ratio=ratio) == graph


# --- insufficient ------------------------------------------------------------


def test_no_citation_flow_is_insufficient_with_a_reason():
    corpus = _Corpus()
    for name in ("A", "B", "C", "D"):
        corpus.group(name, 10)
    graph = learning_map_graph(corpus.works)
    assert graph["insufficient"] is True
    assert graph["reason"] == REASON_NO_CITATION_FLOW
    assert graph["nodes"] == [] and graph["edges"] == []
    assert graph["root"] == TOPIC


def test_too_few_concepts_is_insufficient_not_a_wrong_map():
    corpus = _Corpus().group("A", 10).group("B", 10).cite("B", "A", 10)
    graph = learning_map_graph(corpus.works)
    assert graph["insufficient"] is True
    assert graph["reason"] == REASON_TOO_FEW_CONCEPTS
    assert graph["nodes"] == [] and graph["edges"] == []
    assert 2 < MIN_NODES


def test_empty_and_uncategorised_input():
    empty = learning_map_graph([])
    assert empty["insufficient"] is True and empty["reason"] == REASON_TOO_FEW_CONCEPTS
    graph = learning_map_graph([_work(0, [])])
    assert graph["work_count"] == 0 and graph["insufficient"] is True


def test_min_support_and_max_nodes_bound_the_map():
    corpus = _gnn().group("Tiny", 5).cite("Tiny", "GCN", 5)
    assert "Tiny" not in {n["name"] for n in learning_map_graph(corpus.works)["nodes"]}
    assert "Tiny" in {n["name"] for n in learning_map_graph(corpus.works, min_support=5)["nodes"]}
    # Five concepts tie at 10 works; the cap keeps the first four by name.
    capped = learning_map_graph(_gnn().works, max_nodes=4)
    assert {n["name"] for n in capped["nodes"]} == {
        "Drug discovery",
        "GAT",
        "GCN",
        "Graph theory",
    }


def test_params_are_recorded():
    graph = learning_map_graph(_gnn().works, min_flow=3, ratio=1.5, min_support=6, max_nodes=9)
    assert graph["params"] == {"min_flow": 3, "ratio": 1.5, "min_support": 6, "max_nodes": 9}


# --- nodes, order, roles -----------------------------------------------------


def test_the_query_topic_titles_the_map_instead_of_being_a_node():
    graph = learning_map_graph(_gnn().works)
    assert graph["root"] == TOPIC
    assert TOPIC not in {n["name"] for n in graph["nodes"]}


def test_layers_and_reading_order_follow_the_arrows():
    graph = learning_map_graph(_gnn().works)
    assert _node(graph, "Graph theory")["layer"] == 0
    assert _node(graph, "Message passing")["layer"] == 1
    assert _node(graph, "GCN")["layer"] == 2
    order = [n["name"] for n in sorted(graph["nodes"], key=lambda n: n["order"])]
    assert order.index("Graph theory") < order.index("Message passing") < order.index("GCN")


def test_roles():
    graph = learning_map_graph(_gnn().works)
    assert _node(graph, "Graph theory")["role"] == ROLE_PREREQUISITE
    assert _node(graph, "Message passing")["role"] == ROLE_CORE
    assert _node(graph, "GCN")["role"] == ROLE_METHOD
    # The only concept whose share grows from the first half of the range to
    # the second.
    assert _node(graph, "Drug discovery")["role"] == ROLE_CURRENT
    assert _node(graph, "Drug discovery")["growth"] == 1.0


def test_a_parentless_concept_with_nothing_under_it_is_not_a_prerequisite():
    graph = learning_map_graph(_gnn().group("Benchmarks", 9, year=2016).works)
    benchmarks = _node(graph, "Benchmarks")
    assert benchmarks["layer"] == 0 and benchmarks["role"] != ROLE_PREREQUISITE


def test_a_single_year_corpus_has_no_growth_and_string_dates_are_read():
    corpus = _gnn()
    for w in corpus.works:
        w["published_at"] = "2020-03-01"
    graph = learning_map_graph(corpus.works)
    assert {n["growth"] for n in graph["nodes"]} == {None}
    assert _node(graph, "GCN")["median_year"] == 2020
    for w in corpus.works:
        w["published_at"] = None
    assert {n["median_year"] for n in learning_map_graph(corpus.works)["nodes"]} == {None}


# --- key works ---------------------------------------------------------------


def test_key_work_is_the_most_cited_unless_the_user_already_has_one():
    corpus = _gnn()
    gcn = corpus.groups["GCN"]
    gcn[0].update(cited_by_count=5000, doi="10.1234/top")
    gcn[1].update(cited_by_count=3, doi="10.1234/mine")

    plain = _node(learning_map_graph(corpus.works), "GCN")["key_work"]
    assert plain["doi"] == "10.1234/top" and plain["in_library"] is False

    # A DOI in URL form still matches: library DOIs are normalised too.
    mine = _node(
        learning_map_graph(corpus.works, library_dois=["https://doi.org/10.1234/MINE"]), "GCN"
    )
    assert mine["key_work"]["doi"] == "10.1234/mine"
    assert mine["key_work"]["in_library"] is True


# --- determinism and input shapes --------------------------------------------


def test_shuffled_input_gives_the_same_map():
    works = _gnn().works
    for i, w in enumerate(works):
        w["cited_by_count"] = i % 5  # plenty of citation ties
    expected = learning_map_graph(works)
    rng = random.Random(7)
    for _ in range(5):
        shuffled = works[:]
        rng.shuffle(shuffled)
        assert learning_map_graph(shuffled) == expected


def test_accepts_paper_payloads_as_works_in_range_returns_them():
    works = _gnn().works
    payloads = [
        PaperPayload(
            source="openalex",
            external_id=w["external_id"],
            title=w["title"],
            abstract=None,
            authors=[],
            url=w["url"],
            pdf_url=None,
            published_at=w["published_at"],
            categories=w["categories"],
            cited_by_count=w["cited_by_count"],
            referenced_works=tuple(w["referenced_works"]),
        )
        for w in works
    ]
    assert learning_map_graph(payloads) == learning_map_graph(works)


def test_result_is_json_serialisable_for_report_stats():
    graph = learning_map_graph(_gnn().works)
    assert json.loads(json.dumps(graph)) == graph
