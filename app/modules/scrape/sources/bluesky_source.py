"""Bluesky social feed adapter — account resolution and author feed ingestion.

This module accesses the public Bluesky AppView XRPC API without credentials:
  - Account profile resolution: app.bsky.actor.getProfile
  - Author feed fetching: app.bsky.feed.getAuthorFeed (filter=posts_no_replies)

Exposes standard source adapter contract:
  SOURCE_NAME: str
  search(query, *, max_results) -> list[PaperPayload]
  search_for_keywords(keywords, *, max_results) -> list[PaperPayload]
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import requests
import structlog

from app.modules.scrape.ratelimit import SourceThrottledError, bluesky_slot
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

SOURCE_NAME = "bluesky"
PUBLIC_API_URL = "https://public.api.bsky.app/xrpc"
USER_AGENT = "ScrapeMind/1.0 (+https://github.com/AlperEnesErsu/ScrapeMind)"
_TIMEOUT = 10

_RESOLVE_ERROR = "Could not find that Bluesky account — check the handle or link and try again."
_HASHTAG_RE = re.compile(r"#(\w+)")


def _clean_actor_input(input_val: str) -> str:
    """Normalize a user-provided Bluesky handle, URL, or DID."""
    val = (input_val or "").strip()
    if not val:
        return ""

    if val.startswith("http://") or val.startswith("https://"):
        parsed = urlparse(val)
        # Matches https://bsky.app/profile/<handle_or_did>
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "profile":
            return parts[1].strip()
        if len(parts) == 1:
            return parts[0].strip()

    if val.startswith("@"):
        val = val[1:].strip()

    return val


def resolve_account(handle_or_url: str) -> tuple[str, str, str | None, str | None]:
    """Resolve and validate a Bluesky handle or profile URL.

    Returns:
        (did, handle, display_name, avatar_url)

    Raises:
        ValueError: If the account cannot be found or the input is invalid.
        SourceThrottledError: If the rate limit is exceeded.
    """
    actor = _clean_actor_input(handle_or_url)
    if not actor:
        raise ValueError(_RESOLVE_ERROR)

    if not bluesky_slot():
        raise SourceThrottledError("Bluesky rate limit reached")

    url = f"{PUBLIC_API_URL}/app.bsky.actor.getProfile"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    params = {"actor": actor}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
    except Exception as exc:
        logger.warning("bluesky_resolve_network_error", actor=actor, exc=str(exc))
        raise ValueError(_RESOLVE_ERROR) from exc

    if resp.status_code == 429:
        raise SourceThrottledError("Bluesky rate limit exceeded (HTTP 429)")

    if resp.status_code != 200:
        logger.warning("bluesky_resolve_bad_status", actor=actor, status=resp.status_code)
        raise ValueError(_RESOLVE_ERROR)

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("bluesky_resolve_bad_json", actor=actor, exc=str(exc))
        raise ValueError(_RESOLVE_ERROR) from exc

    did = data.get("did")
    handle = data.get("handle")
    if not did or not handle:
        raise ValueError(_RESOLVE_ERROR)

    display_name = data.get("displayName") or handle
    avatar_url = data.get("avatar")

    return did, handle, display_name, avatar_url


def _parse_post_date(created_at_str: str | None) -> datetime:
    """Parse ISO8601 createdAt to UTC datetime."""
    if not created_at_str:
        return datetime.now(UTC)
    try:
        cleaned = created_at_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return datetime.now(UTC)


def fetch_author_posts(
    actor: str,
    *,
    since: datetime | None = None,
    limit: int = 30,
) -> list[PaperPayload]:
    """Fetch recent main posts from a Bluesky account.

    Args:
        actor: DID or handle of the user.
        since: Optional high-water mark; posts older than or equal to this timestamp are skipped.
        limit: Number of posts to fetch (max 100).

    Returns:
        List of PaperPayload with kind="social" and source="bluesky".
    """
    if not bluesky_slot():
        raise SourceThrottledError("Bluesky rate limit reached")

    url = f"{PUBLIC_API_URL}/app.bsky.feed.getAuthorFeed"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    params: dict[str, Any] = {
        "actor": actor,
        "filter": "posts_no_replies",
        "limit": min(limit, 100),
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
    except Exception as exc:
        logger.warning("bluesky_feed_network_error", actor=actor, exc=str(exc))
        return []

    if resp.status_code == 429:
        raise SourceThrottledError("Bluesky rate limit exceeded (HTTP 429)")

    if resp.status_code != 200:
        logger.warning("bluesky_feed_bad_status", actor=actor, status=resp.status_code)
        return []

    try:
        data = resp.json()
    except Exception:
        logger.warning("bluesky_feed_bad_json", actor=actor)
        return []

    feed = data.get("feed", [])
    payloads: list[PaperPayload] = []

    for item in feed:
        post = item.get("post")
        if not post or not isinstance(post, dict):
            continue

        uri = post.get("uri")
        if not uri:
            continue

        author = post.get("author", {})
        handle = author.get("handle") or actor
        display_name = author.get("displayName") or f"@{handle}"

        record = post.get("record", {})
        text = (record.get("text") or "").strip()
        created_at = _parse_post_date(record.get("createdAt"))

        if since is not None and created_at <= since:
            continue

        rkey = uri.split("/")[-1]
        post_url = f"https://bsky.app/profile/{handle}/post/{rkey}"

        if text:
            first_line = text.split("\n", 1)[0].strip()
            if len(first_line) > 100:
                title = first_line[:97].rstrip() + "..."
            else:
                title = first_line
        else:
            title = f"Post by @{handle}"

        abstract_parts = [text] if text else []
        embed = post.get("embed")
        if isinstance(embed, dict) and "external" in embed:
            ext = embed.get("external", {})
            ext_title = ext.get("title")
            ext_desc = ext.get("description")
            if ext_title or ext_desc:
                note = f"🔗 {ext_title or ''}: {ext_desc or ''}".strip(" :")
                abstract_parts.append(note)

        abstract = "\n\n".join(abstract_parts) if abstract_parts else None

        tags = [m.lower() for m in _HASHTAG_RE.findall(text)]
        categories = ["bluesky", "social"] + tags

        payloads.append(
            PaperPayload(
                source=SOURCE_NAME,
                external_id=uri,
                title=title,
                abstract=abstract,
                authors=[display_name],
                url=post_url,
                pdf_url=None,
                published_at=created_at,
                categories=categories,
                kind="social",
            )
        )

    return payloads


def search(query: str, *, max_results: int = 10) -> list[PaperPayload]:
    """Source adapter contract compatibility — Bluesky ingestion is subscription-based."""
    return []


def search_for_keywords(keywords: list[str], *, max_results: int = 10) -> list[PaperPayload]:
    """Source adapter contract compatibility — Bluesky posts are ingested per-user account."""
    return []
