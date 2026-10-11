"""TeX in titles and abstracts is typeset in the browser by KaTeX.

The typesetting itself is JavaScript (scrape/static/js/math.js) and is not run
here. What the server owns is checked instead: which containers are marked
for it, that the marked text is still the source exactly as stored, that the
CDN files arrive with integrity only on the pages that show abstracts, and
that the CSP admits them.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import pytest

from app.modules.scrape.models import Paper, UserPaper

KATEX = "https://cdn.jsdelivr.net/npm/katex@0.16.11/"
TEX_ABSTRACT = r"We sweep $E_{\mathrm{inc}}/E_{\mathrm{mat}}\in[10^{-2},10^{2}]$ and gain $3.58\%$."
TEX_TITLE = r"Bounds on $\mathcal{N}=4$ amplitudes"


@pytest.fixture
def papers(db, auth_client):
    """One scholarly paper with TeX, one news item that talks about money."""
    _client, uid = auth_client
    rows = {}
    for key, kind, title, abstract in (
        ("paper", None, TEX_TITLE, TEX_ABSTRACT),
        ("news", "news", "Startup raises $5 million", "It costs $5 and $10 a month."),
    ):
        paper = Paper(
            source="openalex" if kind is None else "rss",
            external_id=f"math-{key}-{uuid.uuid4().hex[:8]}",
            title=title,
            abstract=abstract,
            kind=kind,
            published_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        db.session.add(paper)
        db.session.flush()
        link = UserPaper(user_id=uid, paper_id=paper.id)
        db.session.add(link)
        db.session.commit()
        rows[key] = (paper.id, link.id)
    yield rows
    db.session.rollback()
    for paper_id, link_id in rows.values():
        db.session.query(UserPaper).filter_by(id=link_id).delete()
        db.session.query(Paper).filter_by(id=paper_id).delete()
    db.session.commit()


def _tags(body: str) -> list[str]:
    return re.findall(r"<(?:script|link)\b[^>]*katex@[^>]*>", body)


def _assert_assets(body: str) -> None:
    tags = _tags(body)
    assert any("katex.min.css" in t for t in tags)
    assert any("katex.min.js" in t for t in tags)
    assert any("auto-render.min.js" in t for t in tags)
    for tag in tags:
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/]{64}"', tag), tag
        assert 'crossorigin="anonymous"' in tag, tag
    assert "/papers/static/js/math.js" in body or "js/math.js" in body


def test_the_feed_marks_scholarly_text_and_loads_katex(auth_client, papers):
    client, _uid = auth_client
    body = client.get("/papers/").get_data(as_text=True)

    _assert_assets(body)
    # The stored text reaches the page unchanged, inside a marked container.
    assert re.search(r"<p [^>]*data-math[^>]*>" + re.escape(TEX_ABSTRACT) + "</p>", body)
    assert re.search(r'class="paper-card__title" data-math>\s*' + re.escape(TEX_TITLE), body)


def test_news_is_not_marked_so_its_dollars_stay_dollars(auth_client, papers):
    client, _uid = auth_client
    body = client.get("/papers/").get_data(as_text=True)

    match = re.search(r"<p [^>]*>It costs \$5 and \$10 a month\.</p>", body)
    assert match, "the news abstract should be on the page"
    assert "data-math" not in match.group(0)
    title = re.search(r'<a [^>]*class="paper-card__title"[^>]*>\s*Startup raises', body)
    assert title and "data-math" not in title.group(0)


def test_the_detail_page_marks_title_and_abstract(auth_client, papers):
    client, _uid = auth_client
    _paper_id, link_id = papers["paper"]
    body = client.get(f"/papers/{link_id}").get_data(as_text=True)

    _assert_assets(body)
    assert re.search(r'<h2 class="paper-detail__title mb-2" data-math>', body)
    assert re.search(r'<p class="abstract-text" data-math>' + re.escape(TEX_ABSTRACT), body)


def test_library_and_search_load_katex(auth_client, papers):
    client, _uid = auth_client
    for url in ("/library/", "/library/search?q=amplitudes"):
        _assert_assets(client.get(url).get_data(as_text=True))


def test_pages_without_abstracts_do_not_load_katex(auth_client, papers):
    client, _uid = auth_client
    for url in ("/settings/profile", "/library/collections"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert _tags(r.get_data(as_text=True)) == [], url


def test_the_csp_admits_the_katex_files(client):
    headers = client.get("/auth/login").headers
    policy = " ".join(
        headers.get(name, "")
        for name in ("Content-Security-Policy", "Content-Security-Policy-Report-Only")
    )
    script = re.search(r"script-src [^;]*", policy).group(0)
    style = re.search(r"style-src [^;]*", policy).group(0)
    assert KATEX in script.split()
    assert KATEX in style.split()
    # Pinned to the version, never the whole of npm.
    assert "https://cdn.jsdelivr.net " not in policy + " "
