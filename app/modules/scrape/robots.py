"""robots.txt compliance for the general page-scraping path.

`SCRAPING.md` §11 commits us to honouring robots.txt the moment we fetch
anything other than a feed the publisher explicitly offers. That moment is
`web_source`, so this is where the commitment becomes code.

Three decisions worth knowing before touching this:

**We do not use `RobotFileParser.read()`.** It calls `urllib.urlopen` itself:
no timeout, no body cap, and — the one that matters — no SSRF guard. A
robots.txt URL is derived from a user-supplied address, so it is exactly as
untrusted as the page itself. We fetch through `fetcher.get_with_redirects`
like everything else and hand the parser the text via `parse()`.

**Unreachable robots.txt means disallow, not allow.** This is the one place in
the scrape stack that fails *closed* (the Redis limiter fails open, on purpose
— see `ratelimit`). The asymmetry is deliberate: failing open on a rate limit
costs us a quota, failing open here means crawling a site that may have just
told us not to. RFC 9309 says the same — 4xx means "no restrictions", 5xx and
network failures mean "assume disallowed".

**The Redis cache going down must not flip that.** A cache miss is a fetch, not
a denial; only the *fetch* failing denies. Otherwise a Redis blip would look
exactly like every site on the internet disallowing us.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
import structlog

from app.modules.scrape.fetcher import USER_AGENT, fetch_budget, get_with_redirects, read_capped
from app.modules.scrape.ratelimit import acquire_slot

logger = structlog.get_logger()

#: How long a fetched robots.txt stays cached. A day is the usual crawler
#: convention and keeps a nightly run to one robots fetch per host.
DEFAULT_TTL_SECONDS = 24 * 60 * 60

#: robots.txt bodies are tiny. Anything past this is either a misconfigured
#: server or someone serving us their homepage, and neither is worth reading
#: into memory — we cap well below the general body cap.
MAX_ROBOTS_BYTES = 512 * 1024

#: Shown to the user when robots.txt refuses the URL. Deliberately says which
#: rule applied, unlike `net_guard.BLOCKED_MESSAGE` — this is the site's public
#: policy, not information about our network, so there is nothing to leak.
DISALLOWED_MESSAGE = "That site's robots.txt asks crawlers not to fetch this page."
UNREACHABLE_MESSAGE = "Could not read that site's robots.txt, so it was not fetched."

#: Sentinels stored in the cache. An empty body legitimately means "allow all",
#: so "we could not fetch it" needs a value an empty string cannot collide with.
_ALLOW_ALL = ""
_UNREACHABLE = "\x00unreachable"


def robots_url(url: str) -> str:
    """The robots.txt address governing `url` (scheme + host, path `/robots.txt`)."""
    parts = urlparse(url)
    return urlunparse((parts.scheme, parts.netloc, "/robots.txt", "", "", ""))


def _cache_key(url: str) -> str:
    parts = urlparse(url)
    return f"robots:{parts.scheme}://{parts.netloc}"


def _fetch_robots(url: str) -> str:
    """Fetch robots.txt for `url`'s host. Returns its text, `_ALLOW_ALL` when
    the host says there are no rules (4xx), or `_UNREACHABLE` on 5xx/network
    failure/oversized body."""
    target = robots_url(url)
    timeout, _max_bytes, allow_private = fetch_budget()
    headers = {"User-Agent": USER_AGENT, "Accept": "text/plain, */*;q=0.8"}

    try:
        resp, _final, hop_status = get_with_redirects(
            target, headers, timeout, allow_private=allow_private
        )
    except requests.RequestException:
        logger.warning("robots_fetch_failed", url=target)
        return _UNREACHABLE
    except Exception:  # noqa: BLE001 — a bad robots.txt must not kill the caller
        logger.exception("robots_fetch_failed", url=target)
        return _UNREACHABLE

    if resp is None:
        # "blocked" is the SSRF guard refusing the host. Treating that as
        # unreachable rather than allow-all keeps the two guards additive.
        logger.warning("robots_fetch_unavailable", url=target, status=hop_status)
        return _UNREACHABLE

    try:
        status = resp.status_code
        if 400 <= status < 500:
            # RFC 9309: no robots.txt (or forbidden) means no restrictions.
            return _ALLOW_ALL
        if status >= 500:
            logger.warning("robots_fetch_server_error", url=target, status=status)
            return _UNREACHABLE
        try:
            raw = read_capped(resp, MAX_ROBOTS_BYTES)
        except requests.RequestException:
            logger.warning("robots_fetch_timeout", url=target)
            return _UNREACHABLE
    finally:
        resp.close()

    if raw is None:
        logger.warning("robots_too_large", url=target, limit=MAX_ROBOTS_BYTES)
        return _UNREACHABLE
    return raw.decode("utf-8", errors="replace")


def _robots_text(url: str) -> str:
    """Cached `_fetch_robots`. A cache failure degrades to a live fetch."""
    from app.modules.scrape.ratelimit import _client  # noqa: PLC0415 — shared Redis handle

    key = _cache_key(url)
    client = _client()
    if client is not None:
        try:
            hit = client.get(key)
            if hit is not None:
                return hit
        except Exception:  # noqa: BLE001 — a cache miss is a fetch, never a denial
            logger.warning("robots_cache_read_failed", key=key)
            client = None

    text = _fetch_robots(url)
    if client is not None:
        try:
            # An unreachable host is cached too, but briefly: it is usually a
            # transient 5xx, and re-fetching robots.txt on every single page of
            # a nightly run would be its own small DoS.
            ttl = DEFAULT_TTL_SECONDS if text != _UNREACHABLE else 300
            client.setex(key, ttl, text)
        except Exception:  # noqa: BLE001
            logger.warning("robots_cache_write_failed", key=key)
    return text


def _parser(text: str) -> RobotFileParser:
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    return rp


def is_allowed(url: str, *, user_agent: str = USER_AGENT) -> tuple[bool, str | None]:
    """May we fetch `url`? Returns `(allowed, reason_if_not)`.

    The reason is user-facing text — see `DISALLOWED_MESSAGE`.
    """
    text = _robots_text(url)
    if text == _UNREACHABLE:
        return False, UNREACHABLE_MESSAGE
    if text == _ALLOW_ALL:
        return True, None
    try:
        if _parser(text).can_fetch(user_agent, url):
            return True, None
    except Exception:  # noqa: BLE001 — a malformed robots.txt is not a licence
        logger.exception("robots_parse_failed", url=url)
        return False, UNREACHABLE_MESSAGE
    logger.info("robots_disallowed", url=url)
    return False, DISALLOWED_MESSAGE


def crawl_delay(url: str, *, user_agent: str = USER_AGENT) -> float | None:
    """The host's declared `Crawl-delay` in seconds, or None if it declares none."""
    text = _robots_text(url)
    if text in (_ALLOW_ALL, _UNREACHABLE):
        return None
    try:
        raw = _parser(text).crawl_delay(user_agent)
    except Exception:  # noqa: BLE001
        return None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def host_slot(url: str) -> bool:
    """Take one per-host request slot, honouring `Crawl-delay` when declared.

    Reuses `ratelimit.acquire_slot` rather than inventing a second limiter —
    the shape ("N per window, shared across workers, fail open") is identical,
    only the bucket differs. A declared Crawl-delay tightens our configured
    default but is never allowed to loosen it: a site asking for 0.1s does not
    get to talk us into hammering it.
    """
    from app.modules.scrape.fetcher import cfg  # noqa: PLC0415 — avoids a cycle at import

    netloc = urlparse(url).netloc
    if not netloc:
        return True

    limit = int(cfg("SCRAPE_RATE_HOST_PER_MIN", 10))
    delay = crawl_delay(url)
    if delay and delay > 0:
        limit = min(limit, max(1, int(60 // delay)))
    return acquire_slot(f"host:{netloc}", limit, 60)
