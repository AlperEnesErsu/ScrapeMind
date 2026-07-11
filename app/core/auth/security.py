"""Auth security helpers — open-redirect guard + constant-time login padding.

Kept dependency-free (stdlib only) so it can be imported from anywhere in
the auth flow without circular-import risk.
"""

from __future__ import annotations

from urllib.parse import urlparse


def is_safe_next_url(target: str | None) -> bool:
    """True only for a same-site *relative* path.

    We refuse anything that could send the user off-site after login:
      * absolute URLs (have a scheme or netloc) — ``https://evil.com``
      * protocol-relative URLs — ``//evil.com``
      * backslash tricks some browsers normalise to ``//`` — ``/\\evil.com``
      * non-path values that don't start with a single ``/``

    Only a leading-single-slash path (``/papers/``, ``/library/?view=notes``)
    passes. That's enough for our in-app ``?next=`` redirects.
    """
    if not target:
        return False
    if not target.startswith("/"):
        return False
    if target.startswith("//") or target.startswith("/\\"):
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


def safe_next_or(target: str | None, fallback: str) -> str:
    """Return `target` if it's a safe same-site path, else `fallback`."""
    return target if is_safe_next_url(target) else fallback
