"""The learning map's graph (Faz 9.1) — a topic's concepts in prerequisite order.

Pure: no database, no network, no LLM, no Flask. `learning_map_graph` takes the
works a report already harvested and returns the numeric backbone that
`Report.stats["graph"]` will hold (docs/PHASE9.md §5). Everything a reader
sees as structure -- which concepts exist, which arrow points where, in which
order to read them -- is decided here, from counts. The LLM (9.3) only names
and explains nodes on top of it and has no way to add or remove an edge; this
is Faz 6's "numbers first, narrative second" applied to the one place where
a fluent wrong answer would be hardest to notice.

How an arrow is decided (§3.2): subsumption (Sanderson & Croft, 1999). With
`works(X)` the set of works carrying concept X,

    P(A|B) = |works(A) ∩ works(B)| / |works(B)|

and A -> B ("A subsumes B", read A first) when

    P(A|B) >= threshold   and   P(B|A) < 1   and   P(A|B) > P(B|A).

The last clause is not in the paper. It makes the relation antisymmetric
(two concepts that each mostly contain the other get no arrow rather than
two), and because P(A|B) > P(B|A) is the same statement as
|works(A)| > |works(B)|, every arrow points from a more frequent concept to a
strictly less frequent one. Support strictly decreases along every path, so
the graph is acyclic by construction -- there is no cycle check to get wrong.

Then the transitive reduction: A->B, B->C and A->C keeps only the first two,
so the map reads as a tree-like path rather than a hairball.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from app.modules.scrape.doi import normalize_doi

#: A concept must appear in this many works to become a node. Below it,
#: P(A|B) is computed over a handful of works and says little.
DEFAULT_MIN_SUPPORT = 8
#: P(A|B) at or above which A is taken to subsume B.
DEFAULT_THRESHOLD = 0.8
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


@dataclass(frozen=True)
class _Work:
    index: int
    title: str
    year: int | None
    cited_by: int
    doi: str | None
    url: str | None
    concepts: frozenset[str]


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
        out.append(
            _Work(
                index=len(out),
                title=(_field(payload, "title") or "").strip(),
                year=_year(_field(payload, "published_at")),
                cited_by=cited if isinstance(cited, int) and cited >= 0 else 0,
                doi=normalize_doi(_field(payload, "doi")),
                url=_field(payload, "url"),
                concepts=concepts,
            )
        )
    return out


def _subsumption_edges(nodes: list[_Node], threshold: float) -> dict[str, set[str]]:
    """A -> B for every pair the module docstring's rule admits."""
    children: dict[str, set[str]] = {n.name: set() for n in nodes}
    for a in nodes:
        for b in nodes:
            if a is b:
                continue
            shared = len(a.works & b.works)
            if not shared:
                continue
            a_given_b = shared / len(b.works)
            b_given_a = shared / len(a.works)
            if a_given_b >= threshold and b_given_a < 1 and a_given_b > b_given_a:
                children[a.name].add(b.name)
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


def _key_work(node: _Node, works: list[_Work], library_dois: frozenset[str]) -> dict | None:
    candidates = [works[i] for i in node.works]
    if not candidates:
        return None
    # In the library first, then most cited. The tie-break reads the work
    # itself, never its position in the input, so shuffled payloads pick the
    # same key work.
    best = min(
        candidates,
        key=lambda w: (
            not (w.doi is not None and w.doi in library_dois),
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
        "in_library": bool(best.doi and best.doi in library_dois),
    }


def learning_map_graph(
    payloads: Iterable[Any],
    *,
    library_dois: Iterable[str] = (),
    min_support: int = DEFAULT_MIN_SUPPORT,
    threshold: float = DEFAULT_THRESHOLD,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict:
    """The learning map for the works a topic report harvested.

    `payloads` are `PaperPayload`s (or dicts with the same keys): `categories`
    are the concepts -- OpenAlex topics for `works_in_range` -- and `title`,
    `published_at`, `cited_by_count`, `doi`, `url` are read when present.
    `library_dois` marks key works the user already has.

    Deterministic: the same input gives the same output, node and edge order
    included, so a stored map can be compared and tested.

    Returns `{"root", "nodes", "edges", "insufficient", "params", "work_count"}`.
    With fewer than `MIN_NODES` concepts left, `nodes` and `edges` are empty
    and `insufficient` is True.
    """
    params = {"min_support": min_support, "threshold": threshold, "max_nodes": max_nodes}
    works = _works(payloads)
    result: dict[str, Any] = {
        "root": None,
        "nodes": [],
        "edges": [],
        "insufficient": True,
        "params": params,
        "work_count": len(works),
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

    by_name = {n.name: n for n in nodes}
    children = _transitive_reduction(_subsumption_edges(nodes, threshold))
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
        # Structural, not chronological: a parentless concept that subsumes
        # something on the map is what that something builds on. An earlier
        # rule ("parentless and oldest median year") missed exactly these:
        # the broadest concept appears in every year, so its median sits in
        # the middle of the range.
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
            "key_work": _key_work(n, works, library),
        }
        for position, n in enumerate(ordered)
    ]
    result["edges"] = [
        {
            "from": parent,
            "to": kid,
            "p_a_given_b": round(
                len(by_name[parent].works & by_name[kid].works) / len(by_name[kid].works), 4
            ),
        }
        for parent in sorted(children)
        for kid in sorted(children[parent])
    ]
    result["insufficient"] = False
    return result
