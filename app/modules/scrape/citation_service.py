"""Citation graph service for ScrapeMind (Faz 5.4 ③).

Fetches, constructs, and caches interactive 1-hop citation networks
(references cited by this paper + citations citing this paper).

Primary source: OpenAlex API (polite pool, free, open, no key required).
Secondary fallback: Semantic Scholar Graph API.
Graceful fallback: Returns center node from local database if external
APIs are unavailable.
"""

from __future__ import annotations

import copy
import os
from typing import Any

import requests
import structlog

from app.core.cache import get_json, set_json
from app.core.models.user import User
from app.modules.scrape.doi import normalize_doi
from app.modules.scrape.models import Paper, UserPaper
from app.modules.scrape.ratelimit import openalex_slot
from app.modules.scrape.sources import openalex_source

logger = structlog.get_logger()

_OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_SEMANTIC_SCHOLAR_PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper"
_TIMEOUT = 15  # seconds
_CACHE_TTL = 86400  # 24 hours


def _headers() -> dict[str, str]:
    mailto = openalex_source._mailto()
    return {"User-Agent": f"ScrapeMind (mailto:{mailto})"} if mailto else {}


def _to_node(
    item: dict[str, Any],
    node_type: str,
    *,
    is_center: bool = False,
) -> dict[str, Any]:
    """Convert an OpenAlex or normalized work dictionary to a graph node."""
    raw_id = item.get("id") or ""
    short_id = raw_id.rsplit("/", 1)[-1] if "/" in str(raw_id) else str(raw_id)
    title = (item.get("title") or item.get("display_name") or "").strip()
    if not title:
        title = "Untitled"

    year = item.get("publication_year")
    if not year and item.get("publication_date"):
        try:
            year = int(str(item["publication_date"])[:4])
        except (ValueError, TypeError):
            year = None

    authors: list[str] = []
    for a in item.get("authorships") or []:
        author_obj = a.get("author") or {}
        name = author_obj.get("display_name")
        if name:
            authors.append(name)
    if not authors and item.get("authors"):
        for author_entry in item["authors"]:
            if isinstance(author_entry, str):
                authors.append(author_entry)
            elif isinstance(author_entry, dict) and author_entry.get("name"):
                authors.append(author_entry["name"])

    venue = None
    loc = item.get("primary_location") or {}
    src = loc.get("source") or {}
    if src.get("display_name"):
        venue = src["display_name"]
    elif item.get("venue"):
        venue = str(item["venue"])

    raw_doi = item.get("doi")
    norm_doi = normalize_doi(raw_doi)

    url = item.get("doi") or loc.get("landing_page_url") or item.get("url") or raw_id

    citation_count = (
        item.get("cited_by_count")
        if item.get("cited_by_count") is not None
        else item.get("citationCount") or 0
    )

    node_id = short_id or f"work_{abs(hash(title + str(year))) % 10000000}"

    return {
        "id": node_id,
        "title": title.replace("\n", " "),
        "authors": authors[:4],
        "year": year,
        "venue": venue,
        "citation_count": citation_count,
        "doi": norm_doi,
        "url": url,
        "type": node_type,  # 'center' | 'reference' | 'citation'
        "is_center": is_center,
        "in_library": False,
        "user_paper_id": None,
    }


def _resolve_openalex_work(paper: Paper) -> dict[str, Any] | None:
    """Find the OpenAlex work record for a Paper via DOI or title."""
    if not openalex_slot():
        logger.warning("openalex_throttled_in_citation_service")
        return None

    # 1. Try DOI
    norm_doi = normalize_doi(paper.doi) if paper.doi else None
    if norm_doi:
        try:
            resp = requests.get(
                f"{_OPENALEX_WORKS_URL}/https://doi.org/{norm_doi}",
                headers=_headers(),
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("id"):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("openalex_doi_lookup_failed", doi=norm_doi, error=str(exc))

    # 2. Try Title search
    if paper.title and len(paper.title.strip()) > 8:
        try:
            resp = requests.get(
                _OPENALEX_WORKS_URL,
                params={"search": paper.title.strip(), "per-page": 1},
                headers=_headers(),
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                results = resp.json().get("results") or []
                if results and results[0].get("id"):
                    return results[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("openalex_title_lookup_failed", title=paper.title, error=str(exc))

    return None


def _fetch_openalex_references(
    center_work: dict[str, Any], max_count: int = 15
) -> list[dict[str, Any]]:
    """Fetch metadata for works cited by center_work."""
    referenced_ids = center_work.get("referenced_works") or []
    if not referenced_ids:
        return []

    subset = referenced_ids[:max_count]
    short_ids = [r.rsplit("/", 1)[-1] for r in subset if r]
    if not short_ids:
        return []

    if not openalex_slot():
        return []

    try:
        filter_val = "|".join(short_ids)
        resp = requests.get(
            _OPENALEX_WORKS_URL,
            params={"filter": f"openalex_id:{filter_val}", "per-page": len(short_ids)},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        items = resp.json().get("results") or []
        return [_to_node(it, "reference") for it in items if it]
    except Exception as exc:  # noqa: BLE001
        logger.warning("openalex_references_fetch_failed", error=str(exc))
        return []


def _fetch_openalex_citations(
    center_work: dict[str, Any], max_count: int = 15
) -> list[dict[str, Any]]:
    """Fetch top citing works that cite center_work."""
    raw_id = center_work.get("id") or ""
    short_id = raw_id.rsplit("/", 1)[-1] if "/" in str(raw_id) else str(raw_id)
    if not short_id:
        return []

    if not openalex_slot():
        return []

    try:
        resp = requests.get(
            _OPENALEX_WORKS_URL,
            params={
                "filter": f"cites:{short_id}",
                "sort": "cited_by_count:desc",
                "per-page": max_count,
            },
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        items = resp.json().get("results") or []
        return [_to_node(it, "citation") for it in items if it]
    except Exception as exc:  # noqa: BLE001
        logger.warning("openalex_citations_fetch_failed", error=str(exc))
        return []


def _fetch_semantic_scholar_graph(
    paper: Paper, max_references: int = 15, max_citations: int = 15
) -> dict[str, Any] | None:
    """Fallback: Fetch citation graph from Semantic Scholar Graph API."""
    identifier = normalize_doi(paper.doi) if paper.doi else None
    if not identifier:
        if paper.source == "semantic_scholar" and paper.external_id:
            identifier = paper.external_id
        elif paper.source == "arxiv" and paper.external_id:
            identifier = f"ARXIV:{paper.external_id}"

    if not identifier:
        return None

    key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    headers = {"x-api-key": key} if key else {}
    fields = (
        "title,year,authors,citationCount,externalIds,url,"
        "references.title,references.year,references.authors,references.citationCount,references.externalIds,references.url,"
        "citations.title,citations.year,citations.authors,citations.citationCount,citations.externalIds,citations.url"
    )

    try:
        resp = requests.get(
            f"{_SEMANTIC_SCHOLAR_PAPER_URL}/{identifier}",
            params={"fields": fields},
            headers=headers,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data or not data.get("title"):
            return None

        center_node = _to_node(data, "center", is_center=True)
        nodes = [center_node]
        edges: list[dict[str, Any]] = []
        seen_ids = {center_node["id"]}

        # References
        raw_refs = data.get("references") or []
        for ref in raw_refs[:max_references]:
            if not ref or not ref.get("title"):
                continue
            r_node = _to_node(ref, "reference")
            if r_node["id"] not in seen_ids:
                seen_ids.add(r_node["id"])
                nodes.append(r_node)
            edges.append({"source": center_node["id"], "target": r_node["id"], "type": "cites"})

        # Citations
        raw_cits = data.get("citations") or []
        sorted_cits = sorted(
            [c for c in raw_cits if c and c.get("title")],
            key=lambda x: x.get("citationCount") or 0,
            reverse=True,
        )
        for cit in sorted_cits[:max_citations]:
            c_node = _to_node(cit, "citation")
            if c_node["id"] not in seen_ids:
                seen_ids.add(c_node["id"])
                nodes.append(c_node)
            edges.append({"source": c_node["id"], "target": center_node["id"], "type": "cites"})

        return {
            "center_id": center_node["id"],
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "references_count": len([n for n in nodes if n["type"] == "reference"]),
                "citations_count": len([n for n in nodes if n["type"] == "citation"]),
                "total_nodes": len(nodes),
            },
            "source": "semantic_scholar",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("semantic_scholar_graph_fallback_failed", error=str(exc))
        return None


def build_local_center_node(paper: Paper) -> dict[str, Any]:
    """Create a basic center node from local database paper record."""
    norm_doi = normalize_doi(paper.doi) if paper.doi else None
    year = paper.published_at.year if paper.published_at else None
    authors = paper.authors if paper.authors else []
    return {
        "id": f"paper_{paper.id}",
        "title": paper.title,
        "authors": authors[:4],
        "year": year,
        "venue": paper.journal or paper.source,
        "citation_count": paper.cited_by_count or 0,
        "doi": norm_doi,
        "url": paper.url or (f"https://doi.org/{norm_doi}" if norm_doi else None),
        "type": "center",
        "is_center": True,
        "in_library": True,
        "user_paper_id": None,
    }


def get_raw_citation_graph(
    paper: Paper, *, max_references: int = 15, max_citations: int = 15
) -> dict[str, Any]:
    """Fetch or load from cache the raw citation graph topology for a paper.

    Cached in Redis without user-specific decorations for 24h.
    """
    cache_key = f"citation_graph:v1:paper:{paper.id}"
    cached = get_json(cache_key)
    if cached and isinstance(cached, dict) and cached.get("nodes"):
        return cached

    # 1. Try OpenAlex
    center_work = _resolve_openalex_work(paper)
    if center_work:
        center_node = _to_node(center_work, "center", is_center=True)
        if not center_node["title"] and paper.title:
            center_node["title"] = paper.title

        references = _fetch_openalex_references(center_work, max_count=max_references)
        citations = _fetch_openalex_citations(center_work, max_count=max_citations)

        nodes = [center_node]
        edges: list[dict[str, Any]] = []
        seen_ids = {center_node["id"]}

        for ref in references:
            if ref["id"] not in seen_ids:
                seen_ids.add(ref["id"])
                nodes.append(ref)
            edges.append({"source": center_node["id"], "target": ref["id"], "type": "cites"})

        for cit in citations:
            if cit["id"] not in seen_ids:
                seen_ids.add(cit["id"])
                nodes.append(cit)
            edges.append({"source": cit["id"], "target": center_node["id"], "type": "cites"})

        graph = {
            "center_id": center_node["id"],
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "references_count": len(references),
                "citations_count": len(citations),
                "total_nodes": len(nodes),
            },
            "source": "openalex",
        }
        set_json(cache_key, graph, ttl=_CACHE_TTL)
        return graph

    # 2. Try Semantic Scholar fallback
    s2_graph = _fetch_semantic_scholar_graph(
        paper, max_references=max_references, max_citations=max_citations
    )
    if s2_graph:
        set_json(cache_key, s2_graph, ttl=_CACHE_TTL)
        return s2_graph

    # 3. Graceful fallback: local center node only
    center_node = build_local_center_node(paper)
    graph = {
        "center_id": center_node["id"],
        "nodes": [center_node],
        "edges": [],
        "stats": {
            "references_count": 0,
            "citations_count": 0,
            "total_nodes": 1,
        },
        "source": "local_fallback",
        "notice": "no_external_data",
    }
    set_json(cache_key, graph, ttl=600)
    return graph


def decorate_with_user_library(
    graph_data: dict[str, Any], user: User, center_link_id: int
) -> dict[str, Any]:
    """Decorate nodes with user's library status (`in_library`, `user_paper_id`).

    Returns a deep copy of graph_data so the cached topology is never mutated.
    """
    res = copy.deepcopy(graph_data)
    nodes = res.get("nodes") or []

    user_links = (
        UserPaper.query.filter_by(user_id=user.id)
        .join(Paper, UserPaper.paper_id == Paper.id)
        .with_entities(UserPaper.id, Paper.doi, Paper.title)
        .all()
    )

    doi_map: dict[str, int] = {}
    title_map: dict[str, int] = {}
    for up_id, p_doi, p_title in user_links:
        if p_doi:
            norm = normalize_doi(p_doi)
            if norm:
                doi_map[norm] = up_id
        if p_title:
            title_map[p_title.strip().lower()] = up_id

    for node in nodes:
        if node.get("is_center"):
            node["in_library"] = True
            node["user_paper_id"] = center_link_id
            continue

        node_doi = normalize_doi(node.get("doi")) if node.get("doi") else None
        node_title = (node.get("title") or "").strip().lower()

        if node_doi and node_doi in doi_map:
            node["in_library"] = True
            node["user_paper_id"] = doi_map[node_doi]
        elif node_title and node_title in title_map:
            node["in_library"] = True
            node["user_paper_id"] = title_map[node_title]
        else:
            node["in_library"] = False
            node["user_paper_id"] = None

    return res


def get_citation_graph_for_user(paper: Paper, user: User, center_link_id: int) -> dict[str, Any]:
    """Main entry point: gets graph topology and decorates with user library."""
    raw = get_raw_citation_graph(paper)
    return decorate_with_user_library(raw, user, center_link_id)
