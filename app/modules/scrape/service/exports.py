"""BibTeX / CSV export."""

from __future__ import annotations

import structlog

from app.modules.scrape.models import (
    Paper,
    UserPaper,
)

logger = structlog.get_logger()


def to_bibtex(paper: Paper) -> str:
    """Render a paper as a BibTeX entry. arXiv → @misc, anything else → @article.

    Keep it minimal and safe (no template engine) — researchers paste this
    into their .bib so the format must round-trip BibTeX parsers cleanly.
    """
    # Stable cite key: <firstAuthorLastname><year><externalId-suffix>
    year = paper.published_at.year if paper.published_at else "n.d."
    first_author = (paper.authors or ["unknown"])[0]
    last = first_author.split()[-1].lower() if first_author else "unknown"
    ext_tail = (paper.external_id or "").split("/")[-1].replace(".", "")[:8]
    cite_key = f"{last}{year}{ext_tail}"

    entry_type = "misc" if paper.source == "arxiv" else "article"
    authors = " and ".join(paper.authors or [])
    fields = [
        f"  title     = {{{paper.title}}},",
        f"  author    = {{{authors}}}," if authors else None,
        f"  year      = {{{year}}}," if year != "n.d." else None,
        f"  url       = {{{paper.url}}}," if paper.url else None,
        f"  eprint    = {{{paper.external_id}}}," if paper.source == "arxiv" else None,
        "  archivePrefix = {arXiv}," if paper.source == "arxiv" else None,
        f"  abstract  = {{{(paper.abstract or '').strip()}}}," if paper.abstract else None,
    ]
    body = "\n".join(f for f in fields if f)
    return f"@{entry_type}{{{cite_key},\n{body}\n}}\n"


def papers_to_bibtex(papers: list[UserPaper]) -> str:
    """Concatenate BibTeX entries for a set of user_papers — the bulk export
    a researcher drops into Zotero/Mendeley/EndNote."""
    return "\n".join(to_bibtex(up.paper) for up in papers)


def papers_to_csv(papers: list[UserPaper]) -> str:
    """CSV export of a user's papers. Uses the csv module so titles/abstracts
    with commas, quotes, or newlines are quoted correctly (naive f-string
    joining would corrupt the file on the first comma in a title)."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "source",
            "external_id",
            "title",
            "authors",
            "published_at",
            "url",
            "pdf_url",
            "categories",
            "is_favorite",
            "read_later",
            "matched_keyword",
        ]
    )
    for up in papers:
        p = up.paper
        writer.writerow(
            [
                p.source,
                p.external_id,
                p.title,
                "; ".join(p.authors or []),
                p.published_at.date().isoformat() if p.published_at else "",
                p.url or "",
                p.pdf_url or "",
                "; ".join(p.categories or []),
                up.is_favorite,
                up.read_later,
                up.matched_keyword or "",
            ]
        )
    return buf.getvalue()
