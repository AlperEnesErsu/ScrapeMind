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
import subprocess
from typing import Any

import structlog

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
# 1. WEB REACH ADAPTER (Jina Reader)
# ============================================================================

def search_web(query: str, *, max_results: int = 10) -> list[PaperPayload]:
    """Fetch content for a URL or web query via Agent Reach WebChannel."""
    if not query or not query.strip():
        return []

    try:
        from agent_reach.channels.web import WebChannel

        web = WebChannel()

        # If query is a URL, read directly
        target_url = query.strip()
        if not target_url.startswith(("http://", "https://")):
            # If search query, prepend Jina search or fallback URL
            target_url = f"https://s.jina.ai/{urllib.parse.quote(target_url)}" if "urllib" in locals() else target_url

        content = web.read(target_url)
        if not content:
            return []

        # Extract title from Jina Markdown output (usually 'Title: ...')
        lines = content.splitlines()
        title = "Web Result"
        for line in lines[:5]:
            if line.startswith("Title:"):
                title = line.replace("Title:", "").strip()
                break

        payload = PaperPayload(
            source=WEB_SOURCE_NAME,
            external_id=_generate_external_id("web", target_url),
            title=title,
            abstract=content[:1500] if len(content) > 1500 else content,
            authors=["Agent Reach Web"],
            url=target_url,
            pdf_url=None,
            published_at=datetime.datetime.now(datetime.timezone.utc),
            categories=["web"],
            kind="news",
        )
        return [payload]
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

    search_term = f"ytsearch{max_results}:{query.strip()}"
    try:
        res = subprocess.run(
            ["python", "-m", "yt_dlp", "--dump-json", "--flat-playlist", "--no-warnings", search_term],
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

                video_url = f"https://www.youtube.com/watch?v={video_id}" if not video_id.startswith("http") else video_id
                title = item.get("title") or "YouTube Video"
                uploader = item.get("uploader") or item.get("channel") or "YouTube"
                description = item.get("description") or f"YouTube Video by {uploader}"
                duration = item.get("duration")
                duration_str = f" Duration: {duration}s" if duration else ""

                out.append(
                    PaperPayload(
                        source=YOUTUBE_SOURCE_NAME,
                        external_id=_generate_external_id("yt", video_id),
                        title=f"🎥 {title}",
                        abstract=f"{description[:1000]}{duration_str}",
                        authors=[uploader],
                        url=video_url,
                        pdf_url=None,
                        published_at=datetime.datetime.now(datetime.timezone.utc),
                        categories=["video"],
                        kind="video",
                    )
                )
            except json.JSONDecodeError:
                continue

        return out
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

    try:
        # Try using gh CLI
        res = subprocess.run(
            ["gh", "search", "repos", query.strip(), "--limit", str(max_results), "--json", "fullName,description,url,owner,updatedAt"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        if res.returncode == 0 and res.stdout:
            repos = json.loads(res.stdout)
            out: list[PaperPayload] = []
            for repo in repos:
                repo_name = repo.get("fullName") or "GitHub Repo"
                repo_url = repo.get("url") or f"https://github.com/{repo_name}"
                owner = (repo.get("owner") or {}).get("login", "GitHub")
                desc = repo.get("description") or f"GitHub repository: {repo_name}"

                out.append(
                    PaperPayload(
                        source=GITHUB_SOURCE_NAME,
                        external_id=_generate_external_id("gh", repo_name),
                        title=f"📦 {repo_name}",
                        abstract=desc,
                        authors=[owner],
                        url=repo_url,
                        pdf_url=None,
                        published_at=datetime.datetime.now(datetime.timezone.utc),
                        categories=["github"],
                        kind="github",
                    )
                )
            return out
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
