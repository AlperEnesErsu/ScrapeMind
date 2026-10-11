"""The learning map's graph (Faz 9.1) — a topic's concepts in prerequisite order.

Pure: no database, no network, no LLM, no Flask. `learning_map_graph` takes the
works a report already harvested and returns the numeric backbone that
`Report.stats["graph"]` will hold (docs/PHASE9.md §5). Everything a reader
sees as structure -- which concepts exist, which arrow points where, in which
order to read them -- is decided here, from counts. The LLM (9.3) only names
and explains nodes on top of it and has no way to add or remove an edge; this
is Faz 6's "numbers first, narrative second" applied to the one place where
a fluent wrong answer would be hardest to notice.

How an arrow is decided (Faz 9.1b, docs/PHASE9.md §3.2): citation flow inside
the harvested corpus. Every work has an `external_id` and the ids of the works
it cites (`referenced_works`). For node concepts A != B,

    flow(A -> B) = #{(w, r) : w cites r, r in the corpus, r != w,
                              B in concepts(w), A in concepts(r)}

"B's works cite A's works": B builds on A, so A is read first. A candidate
arrow A -> B needs

    flow(A -> B) >= min_flow   and   flow(A -> B) >= ratio * flow(B -> A).

The second clause makes the relation directional: two concepts that cite each
other about equally get no arrow rather than one picked by noise.

Why not subsumption any more. 9.1 drew A -> B when A's works contained B's
(Sanderson & Croft, 1999). Run against live OpenAlex data (§3.5), it drew one
arrow for "graph neural networks" and none for "CRISPR gene editing":
OpenAlex `topics` -- what `categories` holds -- are flat sibling clusters,
none contains another. Citation flow on the same topics drew 16 and 18 arrows
that read like a learning path. The node set did not change; only the edges.

Why acyclicity is now enforced, not given. Subsumption pointed every arrow
from a more frequent concept to a strictly rarer one, so it could not cycle.
Citation flow has no such order: the pairwise ratio rule happily admits
A -> B -> C -> A. The candidates are therefore added greedily, strongest
first (flow desc, reverse flow asc, then names for determinism), and an edge
that would close a cycle is dropped -- an approximate minimum feedback arc
set, which is plenty at 12 nodes and always drops the weakest evidence.

Then the transitive reduction: A->B, B->C and A->C keeps only the first two,
so the map reads as a tree-like path rather than a hairball.

`learning_map_steps` (9.2) is the other direction: a stored graph plus its
narration, flattened into the ordered reading list the report page prints.
Pure as well, so the page's one piece of logic is tested without a request.
`learning_map_canvas` (9.4) trims the same graph down to what the canvas on
that page draws.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from app.modules.scrape.doi import normalize_doi

#: A concept must appear in this many works to become a node. Below it, its
#: citation counts are a handful of pairs and say little.
DEFAULT_MIN_SUPPORT = 8
#: Citation pairs (citing work, cited work) from B to A needed before A -> B
#: is drawn at all.
DEFAULT_MIN_FLOW = 5
#: How many times the flow must outweigh the reverse flow. Mutual citation is
#: not an order.
DEFAULT_RATIO = 2.0
#: Nodes kept, most frequent first. A 680 px map and a short path through it.
DEFAULT_MAX_NODES = 12
#: Fewer nodes than this and the result is reported as insufficient instead
#: of drawn. A wrong map is worse than no map (docs/PHASE9.md §8).
MIN_NODES = 4
#: A concept in at least this share of the works is the topic itself (the
#: query matched on it), not a step on the way. It titles the map instead.
ROOT_SHARE = 0.95

ROLE_PREREQUISITE = "prerequisite"
ROLE_CORE = "core"
ROLE_METHOD = "method"
ROLE_CURRENT = "current"

#: Why `insufficient` is True, so the page can say it instead of a blank map.
REASON_TOO_FEW_CONCEPTS = "too_few_concepts"
REASON_NO_CITATION_FLOW = "no_citation_flow"


@dataclass(frozen=True)
class _Work:
    index: int
    work_id: str | None
    title: str
    year: int | None
    cited_by: int
    doi: str | None
    url: str | None
    concepts: frozenset[str]
    references: tuple[str, ...]


@dataclass
class _Node:
    name: str
    works: set[int] = field(default_factory=set)


def _year(published_at: Any) -> int | None:
    year = getattr(published_at, "year", None)
    if isinstance(year, int):
        return year
    if isinstance(published_at, str) and len(published_at) >= 4 and published_at[:4].isdigit():
        return int(published_at[:4])
    return None


def _field(payload: Any, name: str) -> Any:
    if isinstance(payload, dict):
        return payload.get(name)
    return getattr(payload, name, None)


def _text(value: Any) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _works(payloads: Iterable[Any]) -> list[_Work]:
    out: list[_Work] = []
    for payload in payloads:
        concepts = frozenset(
            c.strip()
            for c in (_field(payload, "categories") or [])
            if isinstance(c, str) and c.strip()
        )
        if not concepts:
            continue
        cited = _field(payload, "cited_by_count")
        refs = _field(payload, "referenced_works") or ()
        out.append(
            _Work(
                index=len(out),
                work_id=_text(_field(payload, "external_id")),
                title=(_field(payload, "title") or "").strip(),
                year=_year(_field(payload, "published_at")),
                cited_by=cited if isinstance(cited, int) and cited >= 0 else 0,
                doi=normalize_doi(_field(payload, "doi")),
                url=_field(payload, "url"),
                concepts=concepts,
                # A dict payload may repeat a reference; a citation is one
                # pair however often it is listed.
                references=tuple(
                    dict.fromkeys(r for r in (_text(x) for x in refs) if r is not None)
                ),
            )
        )
    return out


def _citation_pairs(works: list[_Work]) -> list[tuple[_Work, _Work]]:
    """(citing, cited) for every reference that lands inside the corpus.

    Two payloads with the same id (the same work harvested twice) are both
    cited; the result does not depend on which came first. A work never
    counts as citing itself.
    """
    by_id: dict[str, list[_Work]] = {}
    for work in works:
        if work.work_id is not None:
            by_id.setdefault(work.work_id, []).append(work)
    return [
        (citing, cited)
        for citing in works
        for ref in citing.references
        for cited in by_id.get(ref, ())
        if cited.work_id != citing.work_id
    ]


def _flow(pairs: list[tuple[_Work, _Work]], names: set[str]) -> dict[tuple[str, str], int]:
    """flow[(A, B)]: pairs where a B-work cites an A-work, A != B."""
    flow: dict[tuple[str, str], int] = {}
    for citing, cited in pairs:
        for b in citing.concepts & names:
            for a in cited.concepts & names:
                if a != b:
                    flow[(a, b)] = flow.get((a, b), 0) + 1
    return flow


def _reaches(children: dict[str, set[str]], start: str, target: str) -> bool:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(children[node])
    return False


def _citation_edges(
    names: list[str], flow: dict[tuple[str, str], int], min_flow: int, ratio: float
) -> dict[str, set[str]]:
    """The module docstring's rule, made acyclic greedily, strongest first."""
    candidates = sorted(
        (
            (f, flow.get((b, a), 0), a, b)
            for (a, b), f in flow.items()
            if f >= min_flow and f >= ratio * flow.get((b, a), 0)
        ),
        key=lambda c: (-c[0], c[1], c[2], c[3]),
    )
    children: dict[str, set[str]] = {n: set() for n in names}
    for _, _, a, b in candidates:
        # A -> B closes a cycle exactly when B already reaches A.
        if not _reaches(children, b, a):
            children[a].add(b)
    return children


def _transitive_reduction(children: dict[str, set[str]]) -> dict[str, set[str]]:
    """Drop A->C whenever C is also reachable from A through another child."""

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        stack = list(children[start])
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                stack.extend(children[node])
        return seen

    reduced = {}
    for parent, kids in children.items():
        via_other = set().union(*(reachable(k) for k in kids)) if kids else set()
        reduced[parent] = kids - via_other
    return reduced


def _layers(children: dict[str, set[str]]) -> dict[str, int]:
    """Longest-path depth from a node with no parent. Left-to-right placement."""
    parents: dict[str, set[str]] = {n: set() for n in children}
    for parent, kids in children.items():
        for kid in kids:
            parents[kid].add(parent)

    layer: dict[str, int] = {}

    def depth(node: str) -> int:
        if node not in layer:
            layer[node] = 1 + max((depth(p) for p in parents[node]), default=-1)
        return layer[node]

    for node in children:
        depth(node)
    return layer


def _top_quartile_cut(values: list[float]) -> float:
    """The smallest value still inside the highest quarter of `values`."""
    ordered = sorted(values)
    return ordered[-max(1, math.ceil(len(ordered) / 4))]


def _growth(node: _Node, works: list[_Work], mid: int) -> float | None:
    """Share of the second half's works carrying the concept minus the first
    half's -- `report_service._growing_topics`'s definition, computed from the
    works in hand instead of two extra OpenAlex requests."""
    first = [w for w in works if w.year is not None and w.year <= mid]
    second = [w for w in works if w.year is not None and w.year > mid]
    if not first or not second:
        return None
    share_first = sum(1 for w in first if w.index in node.works) / len(first)
    share_second = sum(1 for w in second if w.index in node.works) / len(second)
    return share_second - share_first


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _key_work(
    node: _Node,
    works: list[_Work],
    library_dois: frozenset[str],
    corpus_citations: dict[int, int],
) -> dict | None:
    """The one work to read for this concept.

    In the user's library first; then the work the corpus itself cites most;
    OpenAlex's global `cited_by_count` only breaks ties. Global counts alone
    picked, for "CRISPR gene editing", a 2019 cannabis genome-editing paper
    OpenAlex credits with 4,224 citations -- a merged-record artefact. A
    count of references *from the other works on the topic* cannot be
    inflated that way, and it is the same signal the arrows come from. With
    no references at all every corpus count is 0 and the order is the
    global one, as before.

    The tie-break reads the work itself, never its position in the input,
    so shuffled payloads pick the same key work.
    """
    candidates = [works[i] for i in node.works]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda w: (
            not (w.doi is not None and w.doi in library_dois),
            -corpus_citations.get(w.index, 0),
            -w.cited_by,
            w.title,
            w.doi or "",
        ),
    )
    return {
        "title": best.title,
        "doi": best.doi,
        "url": best.url,
        "cited_by_count": best.cited_by,
        "corpus_citations": corpus_citations.get(best.index, 0),
        "in_library": bool(best.doi and best.doi in library_dois),
    }


def learning_map_graph(
    payloads: Iterable[Any],
    *,
    library_dois: Iterable[str] = (),
    min_support: int = DEFAULT_MIN_SUPPORT,
    min_flow: int = DEFAULT_MIN_FLOW,
    ratio: float = DEFAULT_RATIO,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict:
    """The learning map for the works a topic report harvested.

    `payloads` are `PaperPayload`s (or dicts with the same keys): `categories`
    are the concepts -- OpenAlex topics for `works_in_range` -- and
    `external_id` + `referenced_works` give the citations the arrows are
    counted from. `title`, `published_at`, `cited_by_count`, `doi`, `url` are
    read when present. `library_dois` marks key works the user already has.

    Deterministic: the same input gives the same output, node and edge order
    included, in whatever order the payloads arrive, so a stored map can be
    compared and tested.

    Returns `{"root", "nodes", "edges", "insufficient", "reason", "params",
    "work_count", "citation_count"}`. `citation_count` is the number of
    citations that stay inside the corpus. With fewer than `MIN_NODES`
    concepts (`reason="too_few_concepts"`) or no arrow at all
    (`reason="no_citation_flow"`) -- a map without arrows is not a path --
    `nodes` and `edges` are empty and `insufficient` is True.
    """
    params = {
        "min_flow": min_flow,
        "ratio": ratio,
        "min_support": min_support,
        "max_nodes": max_nodes,
    }
    works = _works(payloads)
    pairs = _citation_pairs(works)
    corpus_citations: dict[int, int] = {}
    for _citing, cited in pairs:
        corpus_citations[cited.index] = corpus_citations.get(cited.index, 0) + 1
    result: dict[str, Any] = {
        "root": None,
        "nodes": [],
        "edges": [],
        "insufficient": True,
        "reason": REASON_TOO_FEW_CONCEPTS,
        "params": params,
        "work_count": len(works),
        "citation_count": len(pairs),
        "unconnected": [],
    }
    if not works:
        return result

    by_concept: dict[str, _Node] = {}
    for work in works:
        for concept in work.concepts:
            by_concept.setdefault(concept, _Node(concept)).works.add(work.index)

    root_cut = ROOT_SHARE * len(works)
    roots = sorted(
        (n for n in by_concept.values() if len(n.works) >= root_cut),
        key=lambda n: (-len(n.works), n.name),
    )
    result["root"] = roots[0].name if roots else None
    root_names = {n.name for n in roots}

    nodes = sorted(
        (
            n
            for n in by_concept.values()
            if n.name not in root_names and len(n.works) >= min_support
        ),
        key=lambda n: (-len(n.works), n.name),
    )[:max_nodes]
    if len(nodes) < MIN_NODES:
        return result

    names = [n.name for n in nodes]
    flow = _flow(pairs, set(names))
    children = _transitive_reduction(_citation_edges(names, flow, min_flow, ratio))

    # A concept no arrow touches is not a step on a path. Measured on live
    # data (9.5): a "large language models" harvest brought "Protein
    # Structure and Dynamics" along, unconnected -- and as a layer-0 node it
    # sorted to the front of the reading order, ahead of the concepts the
    # map is actually about. Such concepts leave the map but are reported,
    # so nothing is silently dropped.
    connected = {p for p, kids in children.items() if kids} | {
        k for kids in children.values() for k in kids
    }
    result["unconnected"] = [n.name for n in nodes if n.name not in connected]
    if len(connected) < MIN_NODES:
        result["reason"] = REASON_NO_CITATION_FLOW
        return result
    nodes = [n for n in nodes if n.name in connected]
    children = {name: kids for name, kids in children.items() if name in connected}

    layers = _layers(children)
    in_degree = {n.name: 0 for n in nodes}
    for kids in children.values():
        for kid in kids:
            in_degree[kid] += 1

    years = [w.year for w in works if w.year is not None]
    mid = (min(years) + (max(years) - min(years)) // 2) if years else None
    median_year = {
        n.name: (
            median(y for y in (works[i].year for i in n.works) if y is not None)
            if any(works[i].year is not None for i in n.works)
            else None
        )
        for n in nodes
    }
    growth = {
        n.name: (_growth(n, works, mid) if mid is not None and max(years) > min(years) else None)
        for n in nodes
    }

    known_growth = [g for g in growth.values() if g is not None]
    growth_cut = _top_quartile_cut(known_growth) if known_growth else None

    def role(name: str) -> str:
        # Structural, not chronological: a parentless concept that something
        # on the map builds on. An earlier rule ("parentless and oldest
        # median year") missed exactly these: the broadest concept appears in
        # every year, so its median sits in the middle of the range.
        if in_degree[name] == 0 and children[name]:
            return ROLE_PREREQUISITE
        g = growth[name]
        if growth_cut is not None and g is not None and g > 0 and g >= growth_cut:
            return ROLE_CURRENT
        return ROLE_CORE if children[name] else ROLE_METHOD

    library = frozenset(d for d in (normalize_doi(x) for x in library_dois) if d)
    ordered = sorted(nodes, key=lambda n: (layers[n.name], -len(n.works), n.name))
    result["nodes"] = [
        {
            "id": n.name,
            "name": n.name,
            "count": len(n.works),
            "layer": layers[n.name],
            "order": position,
            "role": role(n.name),
            "median_year": median_year[n.name],
            "growth": _rounded(growth[n.name]),
            "key_work": _key_work(n, works, library, corpus_citations),
        }
        for position, n in enumerate(ordered)
    ]
    result["edges"] = [
        {
            "from": parent,
            "to": kid,
            "flow": flow.get((parent, kid), 0),
            "reverse_flow": flow.get((kid, parent), 0),
        }
        for parent in sorted(children)
        for kid in sorted(children[parent])
    ]
    result["insufficient"] = False
    result["reason"] = None
    return result


def _http_url(value: Any) -> str | None:
    text = _text(value)
    if text and text.lower().startswith(("http://", "https://")):
        return text
    return None


def learning_map_steps(graph: Any, narration: Any = None) -> list[dict]:
    """The map as an ordered reading list -- what the report page prints.

    This list is the map's accessible form (docs/PHASE9.md §6): a canvas says
    nothing to a screen reader, so everything the drawing shows is here too,
    as text. `graph` is a stored `learning_map_graph` result and `narration`
    the stored `ai_service.narrate_learning_map` result (or None). Both come
    back from a JSON column, so neither is trusted to have the right shape.

    One step per node, in `order`::

        {"id", "order", "name", "label", "role", "count", "parents",
         "explain", "check_question", "key_work", "key_work_href"}

    `label` is the narrated label, falling back to the OpenAlex `name`.
    `parents` are the labels of the concepts this one builds on (edges
    pointing at it), in reading order -- the arrows, written out.
    `key_work_href` is the key work's own http(s) URL or its DOI link, else
    None: a stored value never becomes an href without a scheme check.
    """
    if not isinstance(graph, dict) or graph.get("insufficient"):
        return []
    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")
    nodes = [
        n
        for n in (raw_nodes if isinstance(raw_nodes, list) else [])
        if isinstance(n, dict) and _text(n.get("id"))
    ]
    raw_cards = narration.get("nodes") if isinstance(narration, dict) else None
    cards: dict = raw_cards if isinstance(raw_cards, dict) else {}

    def card(node_id: str) -> dict:
        found = cards.get(node_id)
        return found if isinstance(found, dict) else {}

    nodes.sort(key=lambda n: n["order"] if isinstance(n.get("order"), int) else 0)
    position = {n["id"]: i for i, n in enumerate(nodes)}
    label = {
        n["id"]: _text(card(n["id"]).get("label")) or _text(n.get("name")) or n["id"] for n in nodes
    }

    parents: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for edge in raw_edges if isinstance(raw_edges, list) else []:
        if not isinstance(edge, dict):
            continue
        src, dst = _text(edge.get("from")), _text(edge.get("to"))
        if src is None or dst is None:
            continue
        if src in position and dst in parents and src != dst and src not in parents[dst]:
            parents[dst].append(src)

    steps = []
    for n in nodes:
        node_id = n["id"]
        key_work = n["key_work"] if isinstance(n.get("key_work"), dict) else None
        href = None
        if key_work:
            href = _http_url(key_work.get("url"))
            doi = normalize_doi(key_work.get("doi"))
            if href is None and doi:
                href = f"https://doi.org/{doi}"
        steps.append(
            {
                "id": node_id,
                "order": n.get("order"),
                "name": _text(n.get("name")) or node_id,
                "label": label[node_id],
                "role": n.get("role"),
                "count": n.get("count"),
                "parents": [label[p] for p in sorted(parents[node_id], key=position.__getitem__)],
                "explain": _text(card(node_id).get("explain")) or "",
                "check_question": _text(card(node_id).get("check_question")) or "",
                "key_work": key_work,
                "key_work_href": href,
            }
        )
    return steps


def learning_map_canvas(graph: Any, steps: list[dict]) -> dict | None:
    """What the page's canvas (9.4) draws, or None when there is nothing to draw.

    Built from the same `steps` the list prints, so the drawing cannot show a
    concept the accessible list leaves out, and its labels match the list's
    word for word. Only what the canvas needs: per node `id`, `label`,
    `role`, `layer`, `order`; per edge `from`, `to`, `flow`. Explanations and
    questions stay out -- the step card copies them from the list items.

    None when there are no steps or no arrow survives: a map without arrows
    is a row of boxes and says less than the list it would sit on top of.
    """
    if not steps or not isinstance(graph, dict):
        return None
    raw_nodes = graph.get("nodes")
    layers = {
        n["id"]: n["layer"]
        for n in (raw_nodes if isinstance(raw_nodes, list) else [])
        if isinstance(n, dict) and isinstance(n.get("id"), str) and isinstance(n.get("layer"), int)
    }
    ids = {s["id"] for s in steps}
    nodes = [
        {
            "id": s["id"],
            "label": s["label"],
            "role": s["role"] if isinstance(s.get("role"), str) else None,
            "layer": layers.get(s["id"], 0),
            "order": s["order"] if isinstance(s.get("order"), int) else i,
        }
        for i, s in enumerate(steps)
    ]
    edges = []
    raw_edges = graph.get("edges")
    for edge in raw_edges if isinstance(raw_edges, list) else []:
        if not isinstance(edge, dict):
            continue
        src, dst = _text(edge.get("from")), _text(edge.get("to"))
        if src is None or dst is None or src == dst or src not in ids or dst not in ids:
            continue
        flow = edge.get("flow")
        edges.append({"from": src, "to": dst, "flow": flow if isinstance(flow, int) else 0})
    if not edges:
        return None
    return {"nodes": nodes, "edges": edges}
