"""Non-RSS page scraping — the discovery ladder (Faz 5.2).

A user points us at a page. We work down four rungs and stop at the first one
that yields items, cheapest and most reliable first:

  1. **RSS autodiscovery** (`<link rel="alternate">`). ADR-0001's finding is
     that most sites believed to have "no RSS" are simply not advertising it
     loudly, so this rung pays for the whole feature. When it hits we hand the
     feed straight to `rss_source` — a publisher-maintained feed beats anything
     we could infer from markup.
  2. **JSON-LD** (`schema.org` `ItemList`/`Blog`/`Article`). Structured data the
     site put there on purpose, usually for search engines. Second because it
     is declared rather than guessed, but it is often absent or partial.
  3. **Repeated blocks.** A listing page is a container whose children share a
     shape. We find the largest such group and read a link + title out of each.
     This is a guess, and it is where a user-supplied CSS selector overrides us.
  4. **Single article** via `trafilatura`. The fallback when the page is one
     piece of prose rather than a list.

**No browser.** JavaScript-rendered pages are out of scope; the reasoning, the
rejected alternatives and the conditions for reopening that are in
[ADR-0001](../../../docs/adr/0001-headless-browser-yok.md).

**Everything extracted is plain text.** `_text()` runs every string through
BeautifulSoup's text extraction and collapses whitespace, so no markup from a
third-party page ever reaches a template. Jinja autoescape is on and no
template in this feature uses `|safe`; both halves are load-bearing.

Fetching goes through `fetcher` (per-hop SSRF revalidation, body cap) and is
gated by `robots` (compliance + per-host politeness) — see `CLAUDE.md` rule 7.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import requests
import structlog
from bs4 import BeautifulSoup

from app.modules.scrape import robots
from app.modules.scrape.fetcher import USER_AGENT, fetch_budget, get_with_redirects, read_capped
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "user_page"

_ACCEPT = "text/html, application/xhtml+xml;q=0.9, */*;q=0.8"

#: A listing needs at least this many sibling items before we believe it is a
#: listing rather than a nav bar or a footer that happens to repeat.
_MIN_BLOCK_ITEMS = 3

#: Cap on items taken from one page — the same reasoning as `rss_source`'s
#: `max_entries`: an archive page can carry hundreds, and ingesting the lot on
#: the first run floods the papers table with years-old news.
_MAX_ITEMS = 40

#: Link text shorter than this is almost always "More", ">>", or an icon.
_MIN_TITLE_CHARS = 12

MODES = ("rss", "jsonld", "blocks", "article")


@dataclass(frozen=True)
class PageFetchResult:
    """Outcome of one page discovery attempt.

    `status` mirrors `rss_source.FeedFetchResult`'s vocabulary and adds
    `robots_denied` (the site's policy refused us) and `empty` (we fetched and
    parsed fine but no rung produced anything — a real outcome, not an error,
    and the message tells the user to try a CSS selector).
    """

    payloads: list[PaperPayload] = field(default_factory=list)
    status: str = "ok"
    mode: str | None = None
    feed_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    title: str | None = None
    detail: str | None = None


def _text(node) -> str:
    """Plain text from a node, whitespace collapsed. The only way text leaves
    a fetched page in this module."""
    if node is None:
        return ""
    raw = node if isinstance(node, str) else node.get_text(" ", strip=True)
    return " ".join(raw.split())


def _abs_url(base: str, href: str | None) -> str | None:
    if not href:
        return None
    joined = urljoin(base, href.strip())
    parts = urlparse(joined)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return joined


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    for candidate in (raw, raw[:19], raw[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _payload(url: str, title: str, *, abstract: str | None, published_at) -> PaperPayload:
    return PaperPayload(
        source=SOURCE_NAME,
        external_id=url,
        title=title[:500],
        abstract=abstract,
        authors=[],
        url=url,
        pdf_url=None,
        published_at=published_at,
        categories=["web"],
        kind="news",
    )


# ----------------------------------------------------------------------------
# Rung 1 — RSS autodiscovery
# ----------------------------------------------------------------------------


def find_feed_url(soup: BeautifulSoup, base_url: str) -> str | None:
    """The first `<link rel="alternate">` advertising a feed, absolutised."""
    for link in soup.find_all("link"):
        rels = {r.lower() for r in (link.get("rel") or [])}
        if "alternate" not in rels:
            continue
        type_ = (link.get("type") or "").lower()
        if "rss" not in type_ and "atom" not in type_ and "xml" not in type_:
            continue
        found = _abs_url(base_url, link.get("href"))
        if found:
            return found
    return None


# ----------------------------------------------------------------------------
# Rung 2 — JSON-LD
# ----------------------------------------------------------------------------


def _jsonld_blobs(soup: BeautifulSoup) -> list:
    out = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            parsed = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue  # a broken blob is common and must not abort discovery
        out.extend(parsed if isinstance(parsed, list) else [parsed])
    return out


def _jsonld_items(blob, base_url: str) -> list[PaperPayload]:
    """Walk one JSON-LD object for anything article-shaped.

    Handles the two common carriers: an `ItemList` of `listElement`s, and a
    bare `Article`/`BlogPosting`. `@graph` is unwrapped because that is how
    several CMSes emit everything at once.
    """
    out: list[PaperPayload] = []
    if not isinstance(blob, dict):
        return out

    for nested in blob.get("@graph") or []:
        out.extend(_jsonld_items(nested, base_url))

    for element in blob.get("itemListElement") or []:
        if isinstance(element, dict):
            out.extend(_jsonld_items(element.get("item") or element, base_url))

    type_ = blob.get("@type") or ""
    types = {type_} if isinstance(type_, str) else set(type_)
    if not types & {"Article", "NewsArticle", "BlogPosting", "Report", "ScholarlyArticle"}:
        return out

    url = _abs_url(base_url, blob.get("url") or blob.get("@id"))
    headline = _text(blob.get("headline") or blob.get("name") or "")
    if not url or not headline:
        return out
    out.append(
        _payload(
            url,
            headline,
            abstract=_text(blob.get("description") or "") or None,
            published_at=_parse_date(blob.get("datePublished") or blob.get("dateCreated")),
        )
    )
    return out


def from_jsonld(soup: BeautifulSoup, base_url: str) -> list[PaperPayload]:
    out: list[PaperPayload] = []
    for blob in _jsonld_blobs(soup):
        out.extend(_jsonld_items(blob, base_url))
    return out


# ----------------------------------------------------------------------------
# Rung 3 — repeated blocks
# ----------------------------------------------------------------------------


def _signature(node) -> str:
    """A crude shape key: tag plus class list. Two siblings rendered by the
    same template almost always share both."""
    classes = " ".join(sorted(node.get("class") or []))
    return f"{node.name}.{classes}"


def _item_from_block(block, base_url: str) -> PaperPayload | None:
    anchor = block if block.name == "a" else block.find("a", href=True)
    if anchor is None:
        return None
    url = _abs_url(base_url, anchor.get("href"))
    if not url:
        return None

    heading = block.find(["h1", "h2", "h3", "h4"])
    title = _text(heading) or _text(anchor)
    if len(title) < _MIN_TITLE_CHARS:
        return None

    time_tag = block.find("time")
    published = _parse_date(time_tag.get("datetime") if time_tag else None) or _parse_date(
        _text(time_tag) if time_tag else None
    )

    paragraph = block.find("p")
    summary = _text(paragraph)
    if summary == title:
        summary = ""
    return _payload(url, title, abstract=summary or None, published_at=published)


def from_blocks(
    soup: BeautifulSoup, base_url: str, *, selector: str | None = None
) -> list[PaperPayload]:
    """Read items out of the largest group of same-shaped siblings.

    `selector` short-circuits the heuristic entirely — that is the "field
    selector" half of the feature: when our guess is wrong the user names the
    container instead of us guessing harder.
    """
    if selector:
        try:
            blocks = soup.select(selector)
        except Exception:  # noqa: BLE001 — a bad selector is user input, not a bug
            logger.info("web_selector_invalid", selector=selector)
            return []
        return _dedupe([_item_from_block(b, base_url) for b in blocks])

    groups: dict[tuple[int, str], list] = {}
    for node in soup.find_all(["article", "li", "div"]):
        parent = node.parent
        if parent is None:
            continue
        groups.setdefault((id(parent), _signature(node)), []).append(node)

    best: list[PaperPayload] = []
    for blocks in groups.values():
        if len(blocks) < _MIN_BLOCK_ITEMS:
            continue
        items = _dedupe([_item_from_block(b, base_url) for b in blocks])
        # Prefer the group that yields the most real items, not the one with
        # the most DOM nodes — a 50-link footer loses to a 6-post listing.
        if len(items) > len(best):
            best = items
    return best


def _dedupe(items: list[PaperPayload | None]) -> list[PaperPayload]:
    seen: set[str] = set()
    out: list[PaperPayload] = []
    for item in items:
        if item is None or item.external_id in seen:
            continue
        seen.add(item.external_id)
        out.append(item)
    return out


# ----------------------------------------------------------------------------
# Rung 4 — single article
# ----------------------------------------------------------------------------


def from_article(html: str, url: str, *, page_title: str | None = None) -> list[PaperPayload]:
    """`trafilatura`'s single-article extraction, as a one-item list."""
    try:
        import trafilatura  # noqa: PLC0415 — heavy optional import, only this rung needs it

        body = trafilatura.extract(html, include_comments=False, include_tables=False)
    except Exception:  # noqa: BLE001 — extraction is best-effort by nature
        logger.info("web_article_extract_failed", url=url)
        return []
    if not body:
        return []
    title = page_title or url
    summary = " ".join(body.split())[:2000]
    return [_payload(url, title, abstract=summary, published_at=None)]


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------


def _fetch(url: str, etag: str | None, last_modified: str | None):
    """GET a page through the shared fetcher. Returns `(result, html)` where
    exactly one is None."""
    headers = {"User-Agent": USER_AGENT, "Accept": _ACCEPT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    timeout, max_bytes, allow_private = fetch_budget()
    try:
        resp, final_url, hop_status = get_with_redirects(
            url, headers, timeout, allow_private=allow_private
        )
    except requests.Timeout:
        return PageFetchResult(status="timeout"), None
    except requests.RequestException:
        return PageFetchResult(status="http_error"), None
    except Exception:  # noqa: BLE001 — a hostile page must not kill ingestion
        logger.exception("web_fetch_failed", url=url)
        return PageFetchResult(status="http_error"), None

    if resp is None:
        return PageFetchResult(status=hop_status), None

    try:
        if resp.status_code == 304:
            return (
                PageFetchResult(status="not_modified", etag=etag, last_modified=last_modified),
                None,
            )
        if resp.status_code >= 400:
            return PageFetchResult(status="http_error"), None
        try:
            raw = read_capped(resp, max_bytes)
        except requests.RequestException:
            return PageFetchResult(status="timeout"), None
        new_etag = resp.headers.get("ETag")
        new_last_modified = resp.headers.get("Last-Modified")
    finally:
        resp.close()

    if raw is None:
        return PageFetchResult(status="too_large"), None
    return (
        PageFetchResult(status="ok", etag=new_etag, last_modified=new_last_modified),
        raw.decode("utf-8", errors="replace"),
    )


def discover(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    mode: str | None = None,
    selector: str | None = None,
) -> PageFetchResult:
    """Fetch `url` and walk the discovery ladder. Never raises for a bad page.

    `mode` pins a rung once one is known to work for this page, so a site that
    briefly stops advertising its feed does not silently downgrade to the
    guessing heuristic and start producing different items.
    """
    allowed, reason = robots.is_allowed(url)
    if not allowed:
        return PageFetchResult(status="robots_denied", detail=reason)
    if not robots.host_slot(url):
        return PageFetchResult(status="timeout", detail="Rate limit for that host.")

    result, html = _fetch(url, etag, last_modified)
    if html is None:
        return result

    soup = BeautifulSoup(html, "lxml")
    page_title = _text(soup.title) or None

    if mode in (None, "rss"):
        feed_url = find_feed_url(soup, url)
        if feed_url:
            from app.modules.scrape.sources.rss_source import (  # noqa: PLC0415 — avoids a cycle
                fetch_feed_conditional,
            )

            feed = fetch_feed_conditional({"key": SOURCE_NAME, "url": feed_url})
            if feed.status == "ok" and feed.payloads:
                return PageFetchResult(
                    payloads=feed.payloads[:_MAX_ITEMS],
                    status="ok",
                    mode="rss",
                    feed_url=feed_url,
                    etag=feed.etag,
                    last_modified=feed.last_modified,
                    title=feed.title or page_title,
                )
            # An advertised feed that does not parse is not a reason to give
            # up on the page — fall through to the markup rungs.
            logger.info("web_advertised_feed_unusable", url=feed_url, status=feed.status)

    rungs = {
        "jsonld": lambda: from_jsonld(soup, url),
        "blocks": lambda: from_blocks(soup, url, selector=selector),
        "article": lambda: from_article(html, url, page_title=page_title),
    }
    for name in ("jsonld", "blocks", "article"):
        if mode not in (None, name):
            continue
        items = rungs[name]()
        if items:
            return PageFetchResult(
                payloads=items[:_MAX_ITEMS],
                status="ok",
                mode=name,
                etag=result.etag,
                last_modified=result.last_modified,
                title=page_title,
            )

    return PageFetchResult(
        status="empty",
        etag=result.etag,
        last_modified=result.last_modified,
        title=page_title,
        detail="Nothing recognisable on that page — try naming the list with a CSS selector.",
    )
