"""Agent Reach source adapter for ScrapeMind.

Integrates Agent Reach capabilities (Web Reader via Jina, YouTube Transcripts,
GitHub Repositories) into ScrapeMind's native `PaperPayload` pipeline.

Exposes standard source adapter contracts:
  SOURCE_NAME: str
  search(query, *, max_results) -> list[PaperPayload]
  search_for_keywords(keywords, *, max_results) -> list[PaperPayload]
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from datetime import UTC
from typing import Any
from urllib.parse import quote

import requests
import structlog

from app.modules.scrape.net_guard import is_public_http_url
from app.modules.scrape.ratelimit import (
    SourceThrottledError,
    github_reach_slot,
    web_reach_slot,
    youtube_reach_slot,
)
from app.modules.scrape.sources.payload import PaperPayload

logger = structlog.get_logger()

# Source Names
WEB_SOURCE_NAME = "web_reach"
YOUTUBE_SOURCE_NAME = "youtube_reach"
GITHUB_SOURCE_NAME = "github_reach"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str | None) -> str | None:
    """Best-effort plain-text abstract — strip HTML tags for card/LLM prompts."""
    if not text:
        return None
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return cleaned or None


def _cfg(key: str, default: Any) -> Any:
    """Read Flask config value safely outside app context."""
    try:
        from flask import current_app

        return current_app.config.get(key, default)
    except Exception:  # noqa: BLE001
        return default


def _generate_external_id(prefix: str, identifier: str) -> str:
    """Generate a stable external_id for deduplication."""
    clean_id = identifier.strip()
    if len(clean_id) <= 200 and not any(c in clean_id for c in ["\n", "\r", " "]):
        return f"{prefix}:{clean_id}"
    digest = hashlib.sha256(clean_id.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


# ============================================================================
# 1. WEB REACH ADAPTER (Jina Reader + Google News RSS Search Fallback)
# ============================================================================


def search_web(query: str, *, max_results: int = 10) -> list[PaperPayload]:
    """Fetch content for a URL (via Jina Reader) or web query (Jina Search / RSS fallback)."""
    clean_query = (query or "").strip()
    if not clean_query:
        return []

    if not web_reach_slot():
        raise SourceThrottledError("Web reach rate limit reached")

    allow_private = False
    try:
        from flask import current_app

        allow_private = bool(current_app.config.get("FEED_ALLOW_PRIVATE_HOSTS", False))
    except Exception:  # noqa: BLE001
        pass

    headers = {"User-Agent": "ScrapeMind/1.0 (+https://github.com/AlperEnesErsu/ScrapeMind)"}
    jina_key = os.getenv("JINA_API_KEY") or _cfg("JINA_API_KEY", None)

    # Branch A: Direct URL provided -> Use Jina Reader (https://r.jina.ai/{url}) with fallback
    if clean_query.startswith(("http://", "https://")):
        ok, _err = is_public_http_url(clean_query, allow_private=allow_private)
        if not ok:
            logger.warning("web_reach_blocked", url=clean_query)
            return []

        jina_url = f"https://r.jina.ai/{clean_query}"
        jina_headers = dict(headers)
        if jina_key:
            jina_headers["Authorization"] = f"Bearer {jina_key}"

        try:
            resp = requests.get(jina_url, headers=jina_headers, timeout=15)
            if resp.status_code == 200 and resp.text:
                content = resp.text
                title = "Web Result"
                for line in content.splitlines()[:10]:
                    line_s = line.strip()
                    if line_s.startswith("Title:"):
                        title = line_s.replace("Title:", "").strip()
                        break
                    elif line_s.startswith("# "):
                        title = line_s.replace("# ", "").strip()
                        break

                return [
                    PaperPayload(
                        source=WEB_SOURCE_NAME,
                        external_id=_generate_external_id("web", clean_query),
                        title=title,
                        abstract=content[:1500] if len(content) > 1500 else content,
                        authors=["Web Reader"],
                        url=clean_query,
                        pdf_url=None,
                        published_at=None,
                        categories=["web"],
                        kind="news",
                    )
                ]
            else:
                logger.warning(
                    "web_reach_jina_reader_error", status_code=resp.status_code, url=clean_query
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("web_reach_jina_reader_failed", url=clean_query, error=str(e))

        # Direct fetch fallback with HTML stripping
        try:
            resp = requests.get(clean_query, headers=headers, timeout=15)
            if resp.status_code != 200 or not resp.text:
                logger.warning(
                    "web_reach_http_error", status_code=resp.status_code, url=clean_query
                )
                return []
            content = resp.text
            match = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE)
            title = match.group(1).strip() if match else "Web Result"
            clean_abstract = _strip_html(content) or ""
            return [
                PaperPayload(
                    source=WEB_SOURCE_NAME,
                    external_id=_generate_external_id("web", clean_query),
                    title=title,
                    abstract=(
                        clean_abstract[:1500] if len(clean_abstract) > 1500 else clean_abstract
                    ),
                    authors=["Web Reader"],
                    url=clean_query,
                    pdf_url=None,
                    published_at=None,
                    categories=["web"],
                    kind="news",
                )
            ]
        except Exception as e:
            logger.error("web_reach_direct_fetch_failed", url=clean_query, error=str(e))
            return []

    # Branch B: Search query (keywords) -> Jina Search (if API key present) or Google News RSS
    if jina_key:
        jina_search_url = f"https://s.jina.ai/{quote(clean_query)}"
        jina_headers = dict(headers)
        jina_headers["Authorization"] = f"Bearer {jina_key}"
        try:
            resp = requests.get(jina_search_url, headers=jina_headers, timeout=15)
            if resp.status_code == 200 and resp.text:
                content = resp.text
                return [
                    PaperPayload(
                        source=WEB_SOURCE_NAME,
                        external_id=_generate_external_id("web", jina_search_url),
                        title=f"Web Search: {clean_query}",
                        abstract=content[:1500] if len(content) > 1500 else content,
                        authors=["Jina Search"],
                        url=jina_search_url,
                        pdf_url=None,
                        published_at=None,
                        categories=["web"],
                        kind="news",
                    )
                ]
            else:
                logger.warning(
                    "web_reach_jina_search_error",
                    status_code=resp.status_code,
                    query=clean_query,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("web_reach_jina_search_failed", query=clean_query, error=str(e))

    # Free web news search fallback via Google News RSS
    try:
        import feedparser

        rss_url = (
            f"https://news.google.com/rss/search?q={quote(clean_query)}&hl=en-US&gl=US&ceid=US:en"
        )
        ok, _err = is_public_http_url(rss_url, allow_private=allow_private)
        if not ok:
            return []

        resp = requests.get(rss_url, headers=headers, timeout=15)
        if resp.status_code != 200 or not resp.content:
            logger.warning(
                "web_reach_rss_search_http_error",
                status_code=resp.status_code,
                query=clean_query,
            )
            return []

        parsed = feedparser.parse(resp.content)
        out: list[PaperPayload] = []
        for entry in parsed.entries[:max_results]:
            external_id = entry.get("id") or entry.get("link")
            title = (entry.get("title") or "").strip()
            link = entry.get("link")
            if not external_id or not title or not link:
                continue

            pub_date = None
            parsed_pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if parsed_pub:
                try:
                    pub_date = datetime.datetime(*parsed_pub[:6], tzinfo=UTC)
                except (TypeError, ValueError):
                    pub_date = None

            out.append(
                PaperPayload(
                    source=WEB_SOURCE_NAME,
                    external_id=_generate_external_id("web", external_id),
                    title=title,
                    abstract=_strip_html(entry.get("summary")),
                    authors=[],
                    url=link,
                    pdf_url=None,
                    published_at=pub_date,
                    categories=["web"],
                    kind="news",
                )
            )
        return out
    except SourceThrottledError:
        raise
    except Exception as e:
        logger.error("web_reach_search_failed", query=clean_query, error=str(e))
        return []


# ============================================================================
# 2. YOUTUBE REACH ADAPTER (yt-dlp)
# ============================================================================


def search_youtube(query: str, *, max_results: int = 5) -> list[PaperPayload]:
    """Search YouTube videos and return PaperPayloads with video links and details."""
    keywords = [k.strip() for k in query.split() if k.strip()]
    if not keywords:
        return []

    if not youtube_reach_slot():
        raise SourceThrottledError("YouTube reach rate limit reached")

    search_term = f"ytsearch{max_results}:{query.strip()}"
    try:
        # Note: --flat-playlist is omitted so yt-dlp extracts full metadata (including upload_date)
        res = subprocess.run(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--dump-json",
                "--no-warnings",
                search_term,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
        )
        if res.returncode != 0 or not res.stdout:
            return []

        out: list[PaperPayload] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                video_id = item.get("id") or item.get("url")
                if not video_id:
                    continue

                video_url = (
                    f"https://www.youtube.com/watch?v={video_id}"
                    if not video_id.startswith("http")
                    else video_id
                )
                raw_title = (item.get("title") or "YouTube Video").strip()
                uploader = item.get("uploader") or item.get("channel") or "YouTube"
                description = item.get("description") or f"YouTube Video by {uploader}"
                duration = item.get("duration")
                duration_str = f" Duration: {duration}s" if duration else ""

                upload_date_raw = item.get("upload_date")
                published_at = None
                if (
                    upload_date_raw
                    and len(str(upload_date_raw)) == 8
                    and str(upload_date_raw).isdigit()
                ):
                    try:
                        published_at = datetime.datetime.strptime(
                            str(upload_date_raw), "%Y%m%d"
                        ).replace(tzinfo=UTC)
                    except ValueError:
                        published_at = None

                out.append(
                    PaperPayload(
                        source=YOUTUBE_SOURCE_NAME,
                        external_id=_generate_external_id("yt", video_id),
                        title=raw_title,
                        abstract=f"{description[:1000]}{duration_str}",
                        authors=[uploader],
                        url=video_url,
                        pdf_url=None,
                        published_at=published_at,
                        categories=["video"],
                        kind="video",
                    )
                )
            except json.JSONDecodeError:
                continue

        return out
    except SourceThrottledError:
        raise
    except Exception as e:
        logger.error("youtube_reach_search_failed", query=query, error=str(e))
        return []


# ============================================================================
# 3. GITHUB REACH ADAPTER (gh / GitHub Search)
# ============================================================================


def search_github(query: str, *, max_results: int = 5) -> list[PaperPayload]:
    """Search GitHub repositories and return PaperPayloads with repo links."""
    if not query or not query.strip():
        return []

    if not github_reach_slot():
        raise SourceThrottledError("GitHub reach rate limit reached")

    try:
        # Try using gh CLI
        res = subprocess.run(
            [
                "gh",
                "search",
                "repos",
                query.strip(),
                "--limit",
                str(max_results),
                "--json",
                "fullName,description,url,owner,updatedAt",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        if res.returncode == 0 and res.stdout:
            repos = json.loads(res.stdout)
            out: list[PaperPayload] = []
            for repo in repos:
                repo_name = (repo.get("fullName") or "GitHub Repo").strip()
                repo_url = repo.get("url") or f"https://github.com/{repo_name}"
                owner = (repo.get("owner") or {}).get("login", "GitHub")
                desc = repo.get("description") or f"GitHub repository: {repo_name}"

                updated_at_raw = repo.get("updatedAt") or repo.get("createdAt")
                published_at = None
                if updated_at_raw:
                    try:
                        published_at = datetime.datetime.fromisoformat(
                            str(updated_at_raw).replace("Z", "+00:00")
                        )
                    except ValueError:
                        published_at = None

                out.append(
                    PaperPayload(
                        source=GITHUB_SOURCE_NAME,
                        external_id=_generate_external_id("gh", repo_name),
                        title=repo_name,
                        abstract=desc,
                        authors=[owner],
                        url=repo_url,
                        pdf_url=None,
                        published_at=published_at,
                        categories=["github"],
                        kind="github",
                    )
                )
            return out
        else:
            logger.warning(
                "github_reach_cli_failed", query=query, returncode=res.returncode, stderr=res.stderr
            )
    except SourceThrottledError:
        raise
    except FileNotFoundError:
        logger.warning(
            "github_cli_not_installed",
            query=query,
            hint="GitHub CLI (gh) is not installed or not in PATH",
        )
    except Exception as e:
        logger.warning("github_reach_cli_failed", query=query, error=str(e))

    return []


# ============================================================================
# 4. MODULE-LEVEL CONVENIENCE HANDLERS
# ============================================================================


def search(query: str, *, max_results: int = 10) -> list[PaperPayload]:
    """Default module search entrypoint — aggregates results across channel adapters."""
    results: list[PaperPayload] = []
    results.extend(youtube_adapter.search(query, max_results=max_results // 3 or 1))
    results.extend(github_adapter.search(query, max_results=max_results // 3 or 1))
    results.extend(web_adapter.search(query, max_results=max_results // 3 or 1))
    return results


def search_for_keywords(keywords: list[str], *, max_results: int = 10) -> list[PaperPayload]:
    """Search across Agent Reach adapters using keywords."""
    clean_kw = [k.strip() for k in keywords if k and k.strip()]
    if not clean_kw:
        return []
    query = " ".join(clean_kw)
    return search(query, max_results=max_results)


# Dedicated channel adapter wrappers satisfying ScrapeMind duck-typed adapter contract
class YouTubeReachAdapter:
    SOURCE_NAME = YOUTUBE_SOURCE_NAME

    def search(self, query: str, *, max_results: int = 5) -> list[PaperPayload]:
        return search_youtube(query, max_results=max_results)

    def search_for_keywords(
        self, keywords: list[str], *, max_results: int = 5
    ) -> list[PaperPayload]:
        clean_kw = [k.strip() for k in keywords if k and k.strip()]
        if not clean_kw:
            return []
        return search_youtube(" ".join(clean_kw), max_results=max_results)


class GitHubReachAdapter:
    SOURCE_NAME = GITHUB_SOURCE_NAME

    def search(self, query: str, *, max_results: int = 5) -> list[PaperPayload]:
        return search_github(query, max_results=max_results)

    def search_for_keywords(
        self, keywords: list[str], *, max_results: int = 5
    ) -> list[PaperPayload]:
        clean_kw = [k.strip() for k in keywords if k and k.strip()]
        if not clean_kw:
            return []
        return search_github(" ".join(clean_kw), max_results=max_results)


class WebReachAdapter:
    SOURCE_NAME = WEB_SOURCE_NAME

    def search(self, query: str, *, max_results: int = 10) -> list[PaperPayload]:
        return search_web(query, max_results=max_results)

    def search_for_keywords(
        self, keywords: list[str], *, max_results: int = 10
    ) -> list[PaperPayload]:
        clean_kw = [k.strip() for k in keywords if k and k.strip()]
        if not clean_kw:
            return []
        return search_web(" ".join(clean_kw), max_results=max_results)


youtube_adapter = YouTubeReachAdapter()
github_adapter = GitHubReachAdapter()
web_adapter = WebReachAdapter()
