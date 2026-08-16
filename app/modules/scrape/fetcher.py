"""Shared HTTP fetch layer: redirect following, SSRF revalidation, body cap.

Why this module exists — and how it squares with `CLAUDE.md` rule 7, which
says adapters must use their own module-level `requests` rather than hiding it
behind a shared wrapper:

    Redirect following and SSRF policy belong to a single fetcher. An adapter
    doing a one-shot GET against a hardcoded API endpoint keeps its own
    module-level `requests`.

The distinction is not stylistic. Rule 7 exists so a canned-response test can
monkeypatch the adapter it is testing. It costs nothing there, because those
adapters call a fixed, trusted host: OpenAlex is never going to 302 anyone to
`169.254.169.254`.

The user-supplied-URL paths are the opposite case. Their security property is
*per-hop revalidation* — `allow_redirects=False` plus a re-check of every
address before it is fetched, because a public-looking URL can redirect into
the network perimeter. That property has to hold identically in every caller,
which is exactly what a copy-pasted loop cannot promise. `rss_source` and
`web_source` are two callers today; a third that quietly forgot a hop check
would be a real vulnerability, not a style violation.

Note for tests: `monkeypatch.setattr(fetcher.requests, "get", ...)` patches the
attribute on the `requests` module object itself, so it is the same object
`rss_source.requests` resolves to — extracting this module did not change what
existing feed tests patch, only where the call now lives.

See ADR-0001 ("Açık soru") for the decision trail.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import requests
import structlog

from app.modules.scrape.net_guard import is_public_http_url

logger = structlog.get_logger()

# Fallbacks used when no Flask app context is available (direct adapter unit
# tests). Inside a worker or request the values come from config — see `cfg`.
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
CONNECT_TIMEOUT = 5
MAX_REDIRECTS = 3

USER_AGENT = "ScrapeMind/1.0 (+https://github.com/birmstf/ScrapeMind)"


def cfg(key: str, default: Any) -> Any:
    """Read a Flask config value, tolerating "no app context".

    This module is otherwise pure I/O, but the fetch budget (timeout, body cap,
    SSRF escape hatch) has to be deployment-configurable and Celery tasks always
    run inside an app context, so a guarded lookup is the least invasive option.
    """
    try:
        from flask import current_app

        return current_app.config.get(key, default)
    except Exception:  # noqa: BLE001 — outside an app context, use the default
        return default


def fetch_budget() -> tuple[tuple[int, int], int, bool]:
    """The three deployment-tunable knobs every user-URL fetch needs:
    `((connect, read) timeout, max_bytes, allow_private_hosts)`.

    Resolved together so a caller cannot pick up the timeout from config and
    then forget the body cap — the two only make sense as a pair.
    """
    timeout = (CONNECT_TIMEOUT, int(cfg("FEED_FETCH_TIMEOUT", DEFAULT_TIMEOUT)))
    max_bytes = int(cfg("FEED_FETCH_MAX_BYTES", DEFAULT_MAX_BYTES))
    allow_private = bool(cfg("FEED_ALLOW_PRIVATE_HOSTS", False))
    return timeout, max_bytes, allow_private


def get_with_redirects(
    url: str,
    headers: dict[str, str],
    timeout: tuple[int, int],
    *,
    allow_private: bool,
):
    """GET `url`, following at most `MAX_REDIRECTS` hops manually.

    Returns `(response | None, final_url, status)` where status is `"ok"`,
    `"blocked"` (SSRF guard refused an address) or `"http_error"`.

    Redirects are followed by hand rather than via `allow_redirects=True` so
    every hop goes back through the SSRF guard — a public URL that 302s to
    `http://169.254.169.254/` would otherwise sail straight past a validation
    that only ever saw the first address.

    The response is returned still streaming (`stream=True`); the caller is
    responsible for `read_capped` + `close`.
    """
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
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


def read_capped(resp, max_bytes: int) -> bytes | None:
    """Accumulate the response body, aborting past `max_bytes` (returns None).

    A user-supplied URL without a cap is an OOM waiting to happen: a 300 MB
    "feed" would take the worker down before feedparser ever saw it.
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
