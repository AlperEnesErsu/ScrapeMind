"""RSS/Atom adapter for industry announcement feeds (OpenAI, Google AI,
Google DeepMind, Hugging Face, ...).

Unlike the academic adapters (arxiv/semantic_scholar/pubmed), these feeds are
NOT per-user keyword searches — the content is identical for every user. The
architecture reflects that: `fetch_feed` is keyword-agnostic (no `search`/
real `search_for_keywords`), and the global ingestion task
(`app/tasks/feed_tasks.py:ingest_all`) fetches each feed exactly once for the
whole deployment. Per-user relevance is scored separately afterwards
(`ai_service.score_feed_relevance` + `service.link_relevant_feed_items`).

Each feed is registered as its own "source" in `AVAILABLE_SOURCES`/
`SOURCE_META` (see `app/modules/scrape/sources/__init__.py`) so the existing
per-user opt-out (`UserSource`) and source-picker UI work unmodified — a user
can mute "OpenAI Blog" without touching arXiv or the other feeds.

Note: Anthropic does not publish an official RSS feed for their news page (no
working `/news/rss.xml`-style URL as of writing — verified live, 404).
Google DeepMind's blog feed was used as the fourth starter source instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import feedparser
import requests
import structlog

from app.modules.scrape.net_guard import is_public_http_url
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

# Fallbacks used when no Flask app context is available (direct adapter unit
# tests). Inside a worker or request the values come from config — see `_cfg`.
_DEFAULT_TIMEOUT = 15
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_CONNECT_TIMEOUT = 5
_MAX_REDIRECTS = 3

_USER_AGENT = "ScrapeMind/1.0 (+https://github.com/birmstf/ScrapeMind)"
_ACCEPT = "application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.8"


def _cfg(key: str, default: Any) -> Any:
    """Read a Flask config value, tolerating "no app context".

    This module is otherwise pure I/O, but the fetch budget (timeout, body cap,
    SSRF escape hatch) has to be deployment-configurable and Celery tasks always
    run inside an app context, so a guarded lookup is the least invasive option.
    """
    try:
        from flask import current_app

        return current_app.config.get(key, default)
    except Exception:  # noqa: BLE001 — outside an app context; use the default
        return default


# Every entry below was verified live (feedparser fetched real entries) before
# being added. Anthropic News, `openai.com/blog/rss.xml` (redirects — kept
# the canonical /news/ URL instead) and a couple of other blogs (Meta AI,
# Mistral, Microsoft AI, Stability AI) were tried and dropped: no working RSS
# endpoint as of this session.
FEEDS: list[dict[str, str]] = [
    {
        "key": "openai_blog",
        "label": "OpenAI Blog",
        "url": "https://openai.com/news/rss.xml",
        "icon": "bi-cpu",
        "desc": "Product and research announcements from OpenAI",
    },
    {
        "key": "google_ai_blog",
        "label": "Google AI Blog",
        "url": "https://blog.google/technology/ai/rss/",
        "icon": "bi-google",
        "desc": "AI news and product updates from Google",
    },
    {
        "key": "deepmind_blog",
        "label": "Google DeepMind Blog",
        "url": "https://deepmind.google/blog/rss.xml",
        "icon": "bi-diagram-2",
        "desc": "Research announcements from Google DeepMind",
    },
    {
        "key": "huggingface_blog",
        "label": "Hugging Face Blog",
        "url": "https://huggingface.co/blog/feed.xml",
        "icon": "bi-emoji-smile",
        "desc": "Open-source ML models, libraries, and community posts",
    },
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str | None) -> str | None:
    """Best-effort plain-text abstract — RSS summaries are often a chunk of
    HTML (links, <p>, images); we just want readable text for the card/LLM
    prompt, not a renderable fragment."""
    if not text:
        return None
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return cleaned or None


def _published_at(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class FeedFetchResult:
    """Outcome of one conditional feed fetch.

    `status` is the caller-facing summary: "ok" (fresh content parsed),
    "not_modified" (server said 304 — nothing to do, zero DB work),
    "blocked" (SSRF guard), "http_error", "timeout", "too_large",
    "parse_error". `etag`/`last_modified` are echoed back so the caller can
    persist them and send them on the next run.
    """

    payloads: list[PaperPayload]
    status: str
    etag: str | None = None
    last_modified: str | None = None
    http_status: int | None = None
    #: The feed's own <title>, used to auto-fill a label when a user adds a
    #: custom feed without naming it.
    title: str | None = None
    #: Raw feedparser entries (`parsed.entries`, capped to `max_entries`),
    #: for callers that need fields `_entries_to_payloads` drops on the floor
    #: (e.g. `media:description`, the entry author, a source-specific id
    #: scheme). Defaulted to `()` so every existing caller/test that only
    #: ever looked at `.payloads` is unaffected. See `youtube_channel_source`
    #: for the motivating consumer.
    entries: tuple = ()


def _get_with_redirects(
    url: str,
    headers: dict[str, str],
    timeout: tuple[int, int],
    *,
    allow_private: bool,
):
    """GET `url`, following at most `_MAX_REDIRECTS` hops manually.

    Redirects are followed by hand rather than via `allow_redirects=True` so
    every hop goes back through the SSRF guard — a public URL that 302s to
    `http://169.254.169.254/` would otherwise sail straight past a validation
    that only ever saw the first address.
    """
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        ok, _err = is_public_http_url(current, allow_private=allow_private)
        if not ok:
            return None, current, "blocked"
        resp = requests.get(
            current,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                return None, current, "http_error"
            current = urljoin(current, location)
            continue
        return resp, current, "ok"
    return None, current, "http_error"


def _read_capped(resp, max_bytes: int) -> bytes | None:
    """Accumulate the response body, aborting past `max_bytes`.

    A feed URL is user-supplied; without a cap a 300 MB "feed" would OOM the
    worker before feedparser ever saw it.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_feed_conditional(
    feed: dict[str, str],
    *,
    max_entries: int = 40,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FeedFetchResult:
    """Fetch + parse one feed, with a real timeout and a conditional GET.

    `feedparser.parse(url)` is deliberately NOT used for the network hop: it
    takes no timeout, so a blackholed feed pins a worker slot forever. We do
    the HTTP ourselves (bounded connect+read timeout, capped body, SSRF-guarded
    redirects) and hand feedparser the bytes.

    Passing the stored `etag`/`last_modified` turns most nightly re-fetches
    into a 304 with no body and no DB work at all.

    `feed["key"]` becomes the persisted `Paper.source` for every entry (so
    per-feed opt-out and per-feed badges work). Never raises for a network or
    parse failure — one broken feed must not kill the whole ingestion run.

    Only the newest `max_entries` items are taken — some feeds (HuggingFace,
    OpenAI) publish 800–1000-item backfills, and ingesting the whole archive
    on the first run would flood the papers table with stale news. Feeds are
    already newest-first; a nightly re-run picks up anything new via dedup.
    """
    url = feed["url"]
    key = feed.get("key", url)

    headers = {"User-Agent": _USER_AGENT, "Accept": _ACCEPT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    timeout = (_CONNECT_TIMEOUT, int(_cfg("FEED_FETCH_TIMEOUT", _DEFAULT_TIMEOUT)))
    max_bytes = int(_cfg("FEED_FETCH_MAX_BYTES", _DEFAULT_MAX_BYTES))
    allow_private = bool(_cfg("FEED_ALLOW_PRIVATE_HOSTS", False))

    try:
        resp, final_url, hop_status = _get_with_redirects(
            url, headers, timeout, allow_private=allow_private
        )
    except requests.Timeout:
        logger.warning("rss_fetch_timeout", key=key, url=url)
        return FeedFetchResult([], "timeout")
    except requests.RequestException:
        logger.warning("rss_fetch_failed", key=key, url=url)
        return FeedFetchResult([], "http_error")
    except Exception:  # noqa: BLE001 — a flaky feed must not kill ingestion
        logger.exception("rss_fetch_failed", key=key, url=url)
        return FeedFetchResult([], "http_error")

    if resp is None:
        if hop_status == "blocked":
            logger.warning("rss_fetch_blocked", key=key, url=final_url)
        return FeedFetchResult([], hop_status)

    try:
        if resp.status_code == 304:
            logger.info("rss_fetch_not_modified", key=key)
            return FeedFetchResult([], "not_modified", etag, last_modified, 304)
        if resp.status_code >= 400:
            logger.warning("rss_fetch_http_error", key=key, status=resp.status_code)
            return FeedFetchResult([], "http_error", http_status=resp.status_code)

        try:
            raw = _read_capped(resp, max_bytes)
        except requests.RequestException:
            logger.warning("rss_fetch_timeout", key=key, url=final_url)
            return FeedFetchResult([], "timeout")
        if raw is None:
            logger.warning("rss_fetch_too_large", key=key, url=final_url, limit=max_bytes)
            return FeedFetchResult([], "too_large", http_status=resp.status_code)

        new_etag = resp.headers.get("ETag")
        new_last_modified = resp.headers.get("Last-Modified")
        http_status = resp.status_code
    finally:
        resp.close()

    try:
        parsed = feedparser.parse(raw)
    except Exception:  # noqa: BLE001
        logger.exception("rss_parse_failed", key=key, url=final_url)
        return FeedFetchResult([], "parse_error", new_etag, new_last_modified, http_status)

    if getattr(parsed, "bozo", False) and not parsed.entries:
        logger.warning(
            "rss_parse_failed", key=key, url=final_url, exc=str(parsed.get("bozo_exception"))
        )
        return FeedFetchResult([], "parse_error", new_etag, new_last_modified, http_status)

    out = _entries_to_payloads(parsed, key, max_entries)
    feed_title = ((getattr(parsed, "feed", None) or {}).get("title") or "").strip() or None
    raw_entries = tuple(parsed.entries[:max_entries])
    logger.info("rss_fetch_done", key=key, hits=len(out))
    return FeedFetchResult(
        out, "ok", new_etag, new_last_modified, http_status, feed_title, raw_entries
    )


def fetch_feed(feed: dict[str, str], *, max_entries: int = 40) -> list[PaperPayload]:
    """Payload-only wrapper over :func:`fetch_feed_conditional`.

    Kept with its original signature on purpose — callers import it
    function-locally and tests monkeypatch it with a plain list-returning
    stub, so the conditional-GET machinery stays opt-in.
    """
    return fetch_feed_conditional(feed, max_entries=max_entries).payloads


def _entries_to_payloads(parsed: Any, key: str, max_entries: int) -> list[PaperPayload]:
    out: list[PaperPayload] = []
    for entry in parsed.entries[:max_entries]:
        external_id = entry.get("id") or entry.get("link")
        title = (entry.get("title") or "").strip()
        link = entry.get("link")
        if not external_id or not title or not link:
            continue
        out.append(
            PaperPayload(
                source=key,
                external_id=external_id,
                title=title,
                abstract=_strip_html(entry.get("summary")),
                authors=[],
                url=link,
                pdf_url=None,
                published_at=_published_at(entry),
                categories=["announcement"],
                kind="news",
            )
        )
    return out


def search_for_keywords(
    keywords: list[str], *, max_results: int = 25
) -> list[PaperPayload]:  # noqa: ARG001
    """No-op — required only so this module satisfies the common source
    interface when it's registered in `AVAILABLE_SOURCES` alongside the
    keyword-search adapters (`scrape_for_user` iterates every enabled source
    calling this).

    RSS feeds are keyword-agnostic and ingested globally on their own
    schedule (`app/tasks/feed_tasks.py:ingest_all`), never through the
    per-user keyword-search flow — so this always returns an empty list
    rather than actually fetching anything.
    """
    return []
