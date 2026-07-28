"""SSRF guard for user-supplied URLs.

Custom RSS feeds are the one place where a user hands us an address that the
*server* then fetches (`service.add_user_feed` validates it, and every nightly
run re-fetches it). Without a guard that is a request-forgery primitive: a feed
pointing at ``http://127.0.0.1:6379/`` or the cloud metadata endpoint
``http://169.254.169.254/`` would be fetched from inside the network perimeter
and its body echoed back into the user's own feed.

Deliberately not a URL allowlist — we resolve the hostname and inspect the
*resolved addresses*, because a public DNS name can point anywhere. The check
runs twice: once when the feed is added (fast user feedback) and again on every
redirect hop at fetch time, since DNS can be re-pointed in between.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger()

# Rejected by name regardless of what they resolve to — cloud metadata services
# are the highest-value SSRF target and some of them answer on names only.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

_ALLOWED_PORTS = frozenset({80, 443})

# Message shown to the user. Deliberately vague about *why* — telling someone
# "that host resolved to 10.0.0.5" turns the guard into a network scanner.
BLOCKED_MESSAGE = "That address can't be used (private or internal host)."


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    # IPv6 transition addresses smuggle an IPv4 address inside a "public"
    # looking one — ::ffff:127.0.0.1 is not caught by is_loopback on v6.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and _is_blocked_ip(mapped):
        return True
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None and _is_blocked_ip(sixtofour):
        return True
    return False


def is_public_http_url(url: str, *, allow_private: bool = False) -> tuple[bool, str | None]:
    """Return ``(ok, error_message)`` for a URL we are about to fetch server-side.

    ``allow_private=True`` short-circuits the whole check — used by
    ``FEED_ALLOW_PRIVATE_HOSTS`` for local development against a feed served
    from localhost, and by the test suite (CI has no outbound DNS, so resolving
    every fixture URL would fail for the wrong reason).

    A hostname that does not resolve is rejected: we cannot fetch it anyway,
    and "unresolvable" must never mean "assume it's fine".
    """
    if allow_private:
        return True, None

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, BLOCKED_MESSAGE

    host = parsed.hostname
    if not host:
        return False, BLOCKED_MESSAGE
    if host.lower().rstrip(".") in _BLOCKED_HOSTNAMES:
        return False, BLOCKED_MESSAGE

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:  # out-of-range port in the URL
        return False, BLOCKED_MESSAGE
    if port not in _ALLOWED_PORTS:
        return False, BLOCKED_MESSAGE

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        logger.warning("ssrf_guard_unresolvable", host=host)
        return False, BLOCKED_MESSAGE

    if not infos:
        return False, BLOCKED_MESSAGE

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False, BLOCKED_MESSAGE
        if _is_blocked_ip(ip):
            logger.warning("ssrf_guard_blocked", host=host)
            return False, BLOCKED_MESSAGE

    return True, None
