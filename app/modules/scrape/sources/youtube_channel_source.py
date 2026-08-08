"""YouTube channel adapter — channel resolution, channel-feed ingestion, and
best-effort transcript fetching.

This is the I/O module only. New-video detection does NOT use yt-dlp (or any
authenticated API) — it reuses YouTube's free, keyless per-channel RSS feed
(`CHANNEL_FEED_URL`), the same `rss_source.fetch_feed_conditional` machinery
every curated/user feed goes through: etag/`If-None-Match` conditional GET,
connect+read timeouts, a streamed body cap, per-hop SSRF revalidation and
`bozo` parse detection. yt-dlp is used only for `fetch_transcript`, once per
video, never for polling — YouTube throttles/blocks yt-dlp far more
aggressively than its own RSS endpoint, so keeping it off the hot polling
path matters.

Like `rss_source`, ingestion here is subscription-based (a user picks
specific channels), not keyword-searched — `search`/`search_for_keywords`
are both no-ops kept only for contract symmetry with the other adapters. See
their docstrings.

Nothing in this module is wired into the scan orchestration loop or Celery
yet — the `UserChannel` model, CRUD, routes, UI and tasks that actually call
`resolve_channel`/`fetch_channel_videos`/`fetch_transcript` land in a later
commit. This commit only makes the I/O correct and covered.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import requests
import structlog

from app.modules.scrape.net_guard import is_public_http_url
from app.modules.scrape.ratelimit import youtube_channel_slot
from app.modules.scrape.sources import rss_source
from app.modules.scrape.sources.payload import PaperPayload
from app.modules.scrape.sources.rss_source import fetch_feed_conditional

logger = structlog.get_logger()

SOURCE_NAME = "youtube_channel"
CHANNEL_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
TRANSCRIPT_MAX_CHARS = 40_000
# YouTube's channel feed only ever carries the latest ~15 uploads anyway —
# asking for more just wastes parse time on entries that don't exist.
_MAX_ENTRIES = 15

_CONNECT_TIMEOUT = 5
# YouTube description field caps out at 5000 chars server-side; matching
# that is a "sane length" for the abstract without an arbitrary guess.
_MAX_DESCRIPTION_CHARS = 5_000

# `--dump-json` has to negotiate a full player-response extraction with
# YouTube (occasionally through a consent/throttle detour) before it prints
# anything, which is slower than the ytsearch flat-playlist listing
# `agent_reach_source.search_youtube` does (15s there). This call isn't on
# any user-facing request path — it runs once per new video from a Celery
# task with its own soft/hard time limits — so the extra headroom costs
# nothing and avoids flagging a slow-but-fine extraction as a hard failure.
_YTDLP_TIMEOUT = 30

_USER_AGENT = rss_source._USER_AGENT

# A generic, non-leaking error for every resolution failure (bad input, SSRF
# guard, network failure, no id found, verify-fetch failed) — same principle
# as `net_guard.BLOCKED_MESSAGE`: a specific reason turns the endpoint into a
# probe an attacker can use to fingerprint the network.
_CHANNEL_ERROR = "Could not find that YouTube channel — check the link and try again."

_BARE_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
_CHANNEL_URL_ID_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/channel/(UC[\w-]{22})(?:[/?#].*)?$",
    re.IGNORECASE,
)
# Ordered: each of these identifies *the page's own* channel. A YouTube
# channel page also carries a `"channelId":"UC…"` for every recommended and
# related channel it links to — twelve distinct ones on a real page, with the
# page's own id nowhere near first. Matching that field subscribed the user to
# whichever channel YouTube happened to recommend, silently and with a
# plausible-looking title, so it is deliberately not in this list: failing to
# resolve is recoverable, subscribing to the wrong channel is not.
_CHANNEL_ID_PATTERNS = (
    re.compile(
        r'<link\s+rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
        re.IGNORECASE,
    ),
    re.compile(r'<meta\s+itemprop="identifier"\s+content="(UC[\w-]{22})"', re.IGNORECASE),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
)
_VIDEO_ID_IN_LINK_RE = re.compile(r"[?&]v=([\w-]+)")

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})

_WS_RE = re.compile(r"\s+")
_VTT_TAG_RE = re.compile(r"<[^>]+>")
_VTT_TIMESTAMP_RE = re.compile(r"-->")


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------


def _direct_channel_id(raw: str) -> str | None:
    """The two input shapes that already carry a channel id with no lookup
    needed: a bare `UC…` id, or a `/channel/UC…` URL."""
    if _BARE_CHANNEL_ID_RE.match(raw):
        return raw
    m = _CHANNEL_URL_ID_RE.match(raw)
    return m.group(1) if m else None


def _normalize_lookup_url(raw: str) -> str | None:
    """Turn a handle/`/c/`/`/user/`/video URL into a `https://www.youtube.com/…`
    URL to fetch. Returns None for anything that isn't recognizably YouTube —
    this adapter only ever fetches youtube.com, never an arbitrary host the
    user typed."""
    candidate = raw
    if candidate.startswith("@"):
        return f"https://www.youtube.com/{candidate}"
    if not re.match(r"^https?://", candidate, re.IGNORECASE):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        if not video_id:
            return None
        return f"https://www.youtube.com/watch?v={video_id}"
    if host in _YOUTUBE_HOSTS:
        path = parsed.path or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"https://www.youtube.com{path}{query}"
    return None


def _resolve_channel_id_from_page(url: str) -> tuple[str | None, str | None]:
    """Fetch a channel/handle/video page and regex the channel id out of it.

    Every YouTube page (channel, handle, or watch page) embeds the owning
    channel's id in its bootstrap JSON (`"channelId":"UC…"`) or, as a
    fallback some templates use instead, a `<meta itemprop="identifier">`
    tag. This is a best-effort scrape of a page we don't control the shape
    of, not a stable API — hence the two-pattern fallback.
    """
    allow_private = bool(rss_source._cfg("FEED_ALLOW_PRIVATE_HOSTS", False))
    ok, _err = is_public_http_url(url, allow_private=allow_private)
    if not ok:
        logger.warning("youtube_channel_resolve_blocked", url=url)
        return None, _CHANNEL_ERROR

    headers = {"User-Agent": _USER_AGENT}
    timeout = (_CONNECT_TIMEOUT, int(rss_source._cfg("FEED_FETCH_TIMEOUT", 15)))
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
    except requests.Timeout:
        logger.warning("youtube_channel_resolve_timeout", url=url)
        return None, _CHANNEL_ERROR
    except requests.RequestException:
        logger.warning("youtube_channel_resolve_failed", url=url)
        return None, _CHANNEL_ERROR
    except Exception:  # noqa: BLE001 — a bad page must not blow up resolution
        logger.exception("youtube_channel_resolve_failed", url=url)
        return None, _CHANNEL_ERROR

    try:
        if resp.status_code >= 400:
            logger.warning("youtube_channel_resolve_http_error", url=url, status=resp.status_code)
            return None, _CHANNEL_ERROR
        max_bytes = int(rss_source._cfg("FEED_FETCH_MAX_BYTES", 5 * 1024 * 1024))
        try:
            raw = rss_source._read_capped(resp, max_bytes)
        except requests.RequestException:
            logger.warning("youtube_channel_resolve_timeout", url=url)
            return None, _CHANNEL_ERROR
    finally:
        resp.close()

    if raw is None:
        logger.warning("youtube_channel_resolve_too_large", url=url, limit=max_bytes)
        return None, _CHANNEL_ERROR

    html_text = raw.decode("utf-8", errors="replace")
    for pattern in _CHANNEL_ID_PATTERNS:
        m = pattern.search(html_text)
        if m:
            return m.group(1), None
    logger.warning("youtube_channel_resolve_no_id_found", url=url)
    return None, _CHANNEL_ERROR


def resolve_channel(raw: str) -> tuple[dict | None, str | None]:
    """Resolve whatever a user pasted (channel URL, handle, `/c/`/`/user/`
    vanity URL, video URL, or a bare `UC…` id) to `{"channel_id", "title",
    "url"}`. Returns `(None, error_message)` on any failure — never raises.

    Every path ends the same way: a live validating fetch of the channel's
    own RSS feed (`fetch_channel_videos`), the same "does this actually
    work" check `service.add_user_feed` does for custom RSS feeds. That both
    confirms the id is real and supplies the channel's own title, so a user
    who pastes a bare id still gets a sensible auto-filled label.
    """
    try:
        raw = (raw or "").strip()
        if not raw:
            return None, _CHANNEL_ERROR

        channel_id = _direct_channel_id(raw)
        if channel_id is None:
            lookup_url = _normalize_lookup_url(raw)
            if lookup_url is None:
                return None, _CHANNEL_ERROR
            channel_id, err = _resolve_channel_id_from_page(lookup_url)
            if err:
                return None, err

        _payloads, status, _etag, _last_modified, feed_title = fetch_channel_videos(channel_id)
        if status != "ok":
            logger.warning(
                "youtube_channel_resolve_verify_failed", channel_id=channel_id, status=status
            )
            return None, _CHANNEL_ERROR

        return {
            "channel_id": channel_id,
            "title": feed_title or channel_id,
            "url": f"https://www.youtube.com/channel/{channel_id}",
        }, None
    except Exception:  # noqa: BLE001 — resolution is user input handling, never raises
        logger.exception("youtube_channel_resolve_unexpected_error")
        return None, _CHANNEL_ERROR


# ---------------------------------------------------------------------------
# Channel feed ingestion
# ---------------------------------------------------------------------------


def _video_id_from_entry(entry: Any) -> str | None:
    """Entry `id` is `yt:video:VIDEOID` — take the part after the last `:`.
    Falls back to the `v=` query param on the entry link when `id` is
    missing entirely."""
    raw_id = entry.get("id")
    if raw_id:
        video_id = raw_id.rsplit(":", 1)[-1].strip()
        if video_id:
            return video_id
    link = entry.get("link") or ""
    m = _VIDEO_ID_IN_LINK_RE.search(link)
    return m.group(1) if m else None


def _entry_description(entry: Any) -> str | None:
    """`<media:group><media:description>` — feedparser's mapping of this is
    version-dependent, so try the flattened attribute first, then the
    generic `summary`, then dig into the raw `media_group` dict."""
    raw = entry.get("media_description") or entry.get("summary")
    if not raw:
        media_group = entry.get("media_group")
        if isinstance(media_group, dict):
            raw = media_group.get("media_description") or media_group.get("description")
    text = rss_source._strip_html(raw)
    if text and len(text) > _MAX_DESCRIPTION_CHARS:
        text = text[:_MAX_DESCRIPTION_CHARS]
    return text


def _entry_published_at(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _entry_to_payload(entry: Any) -> PaperPayload | None:
    video_id = _video_id_from_entry(entry)
    if not video_id:
        return None
    title = (entry.get("title") or "").strip()
    if not title:
        return None
    author = (entry.get("author") or "").strip()
    return PaperPayload(
        source=SOURCE_NAME,
        external_id=video_id,
        title=title,
        abstract=_entry_description(entry),
        authors=[author] if author else [],
        url=f"https://www.youtube.com/watch?v={video_id}",
        pdf_url=None,
        published_at=_entry_published_at(entry),
        categories=["video"],
        kind="video",
        doi=None,
    )


def fetch_channel_videos(
    channel_id: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    max_entries: int = _MAX_ENTRIES,
) -> tuple[list[PaperPayload], str, str | None, str | None, str | None]:
    """Fetch one channel's keyless RSS feed and map its raw entries to
    payloads (`_entries_to_payloads` in `rss_source` drops fields this
    module needs: `kind`, the channel-name author, the media description).

    Returns `(payloads, status, etag, last_modified, title)`. `status` is
    passed straight through from `fetch_feed_conditional`
    (`ok`/`not_modified`/`blocked`/`http_error`/`timeout`/`too_large`/
    `parse_error`) — this function never raises, the caller decides what to
    do with a non-`ok` status.
    """
    feed = {"key": SOURCE_NAME, "url": CHANNEL_FEED_URL.format(channel_id=channel_id)}
    result = fetch_feed_conditional(
        feed, max_entries=max_entries, etag=etag, last_modified=last_modified
    )
    payloads = [p for p in (_entry_to_payload(e) for e in result.entries) if p is not None]
    return payloads, result.status, result.etag, result.last_modified, result.title


# ---------------------------------------------------------------------------
# Transcript fetching (yt-dlp, once per video)
# ---------------------------------------------------------------------------


def _find_track_list(tracks: Any, lang: str) -> list | None:
    """Exact match first, then a prefix match on the language tag — YouTube
    reports variants like `en`, `en-US`, `en-orig` and a user asking for
    `en` should get any of them."""
    if not isinstance(tracks, dict):
        return None
    if lang in tracks:
        return tracks[lang]
    prefix = f"{lang}-"
    for key, val in tracks.items():
        if isinstance(key, str) and key.startswith(prefix):
            return val
    return None


def _pick_track_url(entries: list | None) -> tuple[str, str] | None:
    """Prefer `json3` (easiest to parse cleanly), fall back to `vtt`."""
    by_ext: dict[str, str] = {}
    for e in entries or []:
        if isinstance(e, dict) and e.get("ext") and e.get("url"):
            by_ext.setdefault(e["ext"], e["url"])
    for ext in ("json3", "vtt"):
        if ext in by_ext:
            return by_ext[ext], ext
    return None


def _pick_caption_track(info: dict, languages: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Human-authored `subtitles` beats `automatic_captions` for every
    requested language before falling back to auto captions at all."""
    for source_key in ("subtitles", "automatic_captions"):
        tracks = info.get(source_key)
        if not isinstance(tracks, dict):
            continue
        for lang in languages:
            entries = _find_track_list(tracks, lang)
            if entries:
                picked = _pick_track_url(entries)
                if picked:
                    return picked
    return None, None


def _fetch_caption_body(url: str) -> bytes | None:
    """Caption URLs come back inside yt-dlp's JSON — untrusted remote data —
    so they get the same SSRF guard + capped read as any other server-side
    fetch, not a bare `requests.get`."""
    allow_private = bool(rss_source._cfg("FEED_ALLOW_PRIVATE_HOSTS", False))
    ok, _err = is_public_http_url(url, allow_private=allow_private)
    if not ok:
        logger.warning("youtube_transcript_caption_url_blocked")
        return None

    headers = {"User-Agent": _USER_AGENT}
    timeout = (_CONNECT_TIMEOUT, int(rss_source._cfg("FEED_FETCH_TIMEOUT", 15)))
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
    except requests.RequestException:
        logger.warning("youtube_transcript_caption_fetch_error")
        return None

    try:
        if resp.status_code >= 400:
            logger.warning("youtube_transcript_caption_http_error", status=resp.status_code)
            return None
        max_bytes = int(rss_source._cfg("FEED_FETCH_MAX_BYTES", 5 * 1024 * 1024))
        try:
            return rss_source._read_capped(resp, max_bytes)
        except requests.RequestException:
            logger.warning("youtube_transcript_caption_timeout")
            return None
    finally:
        resp.close()


def _parse_json3(raw: bytes) -> str | None:
    """`{"events": [{"segs": [{"utf8": "..."}]}]}` -> concatenated plain text."""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    parts: list[str] = []
    for event in data.get("events") or []:
        if not isinstance(event, dict):
            continue
        for seg in event.get("segs") or []:
            if isinstance(seg, dict) and seg.get("utf8"):
                parts.append(seg["utf8"])
    text = _WS_RE.sub(" ", "".join(parts)).strip()
    return text or None


def _parse_vtt(raw: bytes) -> str | None:
    """Drop the `WEBVTT` header, cue-number lines, and `-->` timestamp
    lines; strip inline `<...>` tags; de-duplicate consecutive repeated
    lines (auto-caption VTT repeats each line as it scrolls into the next
    cue — without this the transcript triples in size)."""
    text = raw.decode("utf-8", errors="replace")
    out_lines: list[str] = []
    prev: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith(("WEBVTT", "KIND:", "LANGUAGE:", "NOTE", "STYLE", "REGION")):
            continue
        if _VTT_TIMESTAMP_RE.search(stripped):
            continue
        if stripped.isdigit():
            continue
        cleaned = _VTT_TAG_RE.sub("", stripped).strip()
        if not cleaned or cleaned == prev:
            continue
        out_lines.append(cleaned)
        prev = cleaned
    text_out = _WS_RE.sub(" ", " ".join(out_lines)).strip()
    return text_out or None


def fetch_transcript(video_id: str, *, languages: tuple[str, ...] = ("tr", "en")) -> str | None:
    """Best-effort transcript for one video, via `yt-dlp --dump-json`.

    Returns `None` (never raises) whenever a transcript isn't available for
    any reason — yt-dlp not installed, YouTube blocking the request (common
    from datacenter IPs), no captions in the requested languages, a bad
    response. A missing transcript is a degraded result for this one video,
    not a failed source, so unlike the rate-limited academic adapters this
    does not raise `SourceThrottledError` — it isn't called from the
    per-user source-orchestration loop that sentinel exists for.
    """
    if not youtube_channel_slot():
        logger.warning("youtube_transcript_rate_limited", video_id=video_id)
        return None

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--dump-json",
                "--skip-download",
                "--no-warnings",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_YTDLP_TIMEOUT,
        )
    except FileNotFoundError:
        logger.warning("youtube_transcript_ytdlp_missing", video_id=video_id)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("youtube_transcript_timeout", video_id=video_id)
        return None
    except Exception:  # noqa: BLE001 — a transcript miss must not break ingestion
        logger.exception("youtube_transcript_subprocess_failed", video_id=video_id)
        return None

    if proc.returncode != 0 or not proc.stdout:
        logger.warning(
            "youtube_transcript_ytdlp_failed", video_id=video_id, returncode=proc.returncode
        )
        return None

    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("youtube_transcript_json_decode_failed", video_id=video_id)
        return None

    if not isinstance(info, dict):
        logger.warning("youtube_transcript_json_decode_failed", video_id=video_id)
        return None

    caption_url, ext = _pick_caption_track(info, languages)
    if caption_url is None:
        logger.warning("youtube_transcript_no_captions", video_id=video_id, languages=languages)
        return None

    raw = _fetch_caption_body(caption_url)
    if raw is None:
        logger.warning("youtube_transcript_caption_fetch_failed", video_id=video_id)
        return None

    text = _parse_json3(raw) if ext == "json3" else _parse_vtt(raw)
    if not text:
        logger.warning("youtube_transcript_empty_after_parse", video_id=video_id)
        return None

    return text[:TRANSCRIPT_MAX_CHARS]


# ---------------------------------------------------------------------------
# Common source-adapter contract
# ---------------------------------------------------------------------------


def search_for_keywords(
    keywords: list[str], *, max_results: int = 25
) -> list[PaperPayload]:  # noqa: ARG001
    """No-op — same precedent as `rss_source.search_for_keywords`.

    Channels are subscription-based (a per-user `UserChannel` list added in
    a later commit), never keyword-searched, so this always returns an
    empty list. Kept only so the module satisfies the common source-adapter
    contract when registered in `AVAILABLE_SOURCES` — `scrape_for_user`
    iterates every enabled source calling this, and a real no-op is what
    keeps that loop safe rather than one more special case in the caller.
    """
    return []


def search(query: str, *, max_results: int = 25) -> list[PaperPayload]:  # noqa: ARG001
    """No-op, same reasoning as `search_for_keywords` above.

    Unlike `rss_source` (which omits `search`/`SOURCE_NAME` entirely because
    it registers one shared module under many feed-specific keys),
    `youtube_channel_source` is registered under its own single
    `SOURCE_NAME`, so `search` is implemented — trivially — for contract
    symmetry with the keyword-search adapters, even though nothing calls it
    today.
    """
    return []
