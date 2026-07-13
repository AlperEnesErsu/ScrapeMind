"""PubMed (NCBI E-utilities) adapter.

Two-step flow, both unauthenticated-friendly (3 req/s shared; set
NCBI_API_KEY to lift to 10 req/s):

  1. esearch.fcgi — keyword query → newest PMIDs
  2. efetch.fcgi  — PMIDs → article XML (title/abstract/authors/date)

PubMed supports boolean OR natively, so all keywords go in one query.
Same contract as the other adapters: pure I/O, PaperPayload rows out,
requests exceptions propagate for the service layer to isolate.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import requests
import structlog

from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "pubmed"

_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_TIMEOUT = 20  # seconds

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip


def _common_params() -> dict[str, str]:
    params = {"db": "pubmed", "tool": "scrapemind", "email": "noreply@example.com"}
    key = os.getenv("NCBI_API_KEY", "")
    if key:
        params["api_key"] = key
    return params


def _parse_pub_date(article: ET.Element) -> datetime | None:
    """Best-effort publication date from the ArticleDate or PubDate nodes."""
    for xpath in (".//ArticleDate", ".//JournalIssue/PubDate"):
        node = article.find(xpath)
        if node is None:
            continue
        year = node.findtext("Year")
        if not year or not year.isdigit():
            continue
        month_raw = (node.findtext("Month") or "1").strip().lower()
        month = _MONTHS.get(month_raw[:3], None)
        if month is None:
            month = int(month_raw) if month_raw.isdigit() else 1
        day_raw = (node.findtext("Day") or "1").strip()
        day = int(day_raw) if day_raw.isdigit() else 1
        try:
            return datetime(int(year), month, day, tzinfo=UTC)
        except ValueError:
            return datetime(int(year), 1, 1, tzinfo=UTC)
    return None


def _parse_article(pubmed_article: ET.Element) -> PaperPayload | None:
    pmid = pubmed_article.findtext(".//PMID")
    article = pubmed_article.find(".//Article")
    if not pmid or article is None:
        return None
    # itertext() flattens inline markup PubMed embeds in titles (<i>, <sub>…)
    title_node = article.find("ArticleTitle")
    title = "".join(title_node.itertext()).strip() if title_node is not None else ""
    if not title:
        return None

    abstract_parts = [
        "".join(t.itertext()).strip() for t in article.findall(".//Abstract/AbstractText")
    ]
    abstract = "\n\n".join(p for p in abstract_parts if p) or None

    authors: list[str] = []
    for a in article.findall(".//AuthorList/Author"):
        last, fore = a.findtext("LastName"), a.findtext("ForeName")
        if last and fore:
            authors.append(f"{fore} {last}")
        elif last:
            authors.append(last)
        elif a.findtext("CollectiveName"):
            authors.append(a.findtext("CollectiveName"))

    mesh = [
        m.findtext("DescriptorName")
        for m in pubmed_article.findall(".//MeshHeadingList/MeshHeading")
    ]

    return PaperPayload(
        source=SOURCE_NAME,
        external_id=pmid,
        title=title.replace("\n", " "),
        abstract=abstract,
        authors=authors,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        pdf_url=None,  # PubMed hosts abstracts, not PDFs
        published_at=_parse_pub_date(pubmed_article),
        categories=[m for m in mesh if m][:8],
    )


def search(query: str, *, max_results: int = 25) -> list[PaperPayload]:
    """esearch → efetch round-trip. Raises requests exceptions on HTTP
    failure; XML parse errors on a whole response also propagate."""
    query = (query or "").strip()
    if not query:
        return []

    es = requests.get(
        _ESEARCH_URL,
        params={
            **_common_params(),
            "term": query,
            "retmax": min(max_results, 100),
            "retmode": "json",
            "sort": "date",
        },
        timeout=_TIMEOUT,
    )
    es.raise_for_status()
    ids = es.json().get("esearchresult", {}).get("idlist") or []
    if not ids:
        logger.info("pubmed_search_done", query=query, hits=0)
        return []

    ef = requests.get(
        _EFETCH_URL,
        params={**_common_params(), "id": ",".join(ids), "retmode": "xml"},
        timeout=_TIMEOUT,
    )
    ef.raise_for_status()
    root = ET.fromstring(ef.content)

    out: list[PaperPayload] = []
    for node in root.findall(".//PubmedArticle"):
        payload = _parse_article(node)
        if payload is not None:
            out.append(payload)
    logger.info("pubmed_search_done", query=query, hits=len(out))
    return out


def search_for_keywords(keywords: list[str], *, max_results: int = 25) -> list[PaperPayload]:
    """PubMed handles boolean OR natively — one combined query per run."""
    keywords = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not keywords:
        return []
    query = " OR ".join(f'"{kw}"' for kw in keywords)
    return search(query, max_results=max_results)
