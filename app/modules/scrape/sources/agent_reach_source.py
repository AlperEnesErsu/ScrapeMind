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
import json
import re
import subprocess
import sys
from datetime import UTC
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


def _generate_external_id(prefix: str, identifier: str) -> str:
    """Generate a stable external_id for deduplication."""
    clean_id = identifier.strip()
    if len(clean_id) <= 200 and not any(c in clean_id for c in ["\n", "\r", " "]):
        return f"{prefix}:{clean_id}"
    digest = hashlib.sha256(clean_id.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


# ============================================================================
# 1. WEB REACH ADAPTER (HTTP Reader via requests + net_guard)
# ============================================================================


def search_web(query: str, *, max_results: int = 10) -> list[PaperPayload]:
    """Fetch content for a URL or web query via HTTP requests guarded by net_guard."""
    if not query or not query.strip():
        return []

    if not web_reach_slot():
        raise SourceThrottledError("Web reach rate limit reached")

    try:
        target_url = query.strip()
        if not target_url.startswith(("http://", "https://")):
            target_url = f"https://s.jina.ai/{quote(target_url)}"

        allow_private = False
        try:
            from flask import current_app

            allow_private = bool(current_app.config.get("FEED_ALLOW_PRIVATE_HOSTS", False))
        except Exception:  # noqa: BLE001
            pass

        ok, _err = is_public_http_url(target_url, allow_private=allow_private)
        if not ok:
            logger.warning("web_reach_blocked", url=target_url)
            return []

        headers = {"User-Agent": "ScrapeMind/1.0 (+https://github.com/AlperEnesErsu/ScrapeMind)"}
        resp = requests.get(target_url, headers=headers, timeout=15)
        if resp.status_code != 200 or not resp.text:
            return []

        content = resp.text
        lines = content.splitlines()
        title = "Web Result"
        for line in lines[:10]:
            clean_line = line.strip()
            if clean_line.startswith("Title:"):
                title = clean_line.replace("Title:", "").strip()
                break
            elif clean_line.startswith("# "):
                title = clean_line.replace("# ", "").strip()
                break

        if title == "Web Result":
            match = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE)
            if match:
                title = match.group(1).strip()

        payload = PaperPayload(
            source=WEB_SOURCE_NAME,
            external_id=_generate_external_id("web", target_url),
            title=title,
            abstract=content[:1500] if len(content) > 1500 else content,
            authors=["Web Reader"],
            url=target_url,
            pdf_url=None,
            published_at=None,
            categories=["web"],
            kind="news",
        )
        return [payload]
    except SourceThrottledError:
        raise
    except Exception as e:
        logger.error("web_reach_search_failed", query=query, error=str(e))
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
        res = subprocess.run(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--dump-json",
                "--flat-playlist",
                "--no-warnings",
                search_term,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
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


# Common contract handlers for each adapter
def search(query: str, *, max_results: int = 10) -> list[PaperPayload]:
    """Default search entrypoint — aggregates results from available channels."""
    results: list[PaperPayload] = []
    results.extend(search_youtube(query, max_results=max_results // 2))
    results.extend(search_github(query, max_results=max_results // 2))
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
