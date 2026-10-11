"""Every file loaded from a CDN carries Subresource Integrity.

The CSP allows the jsdelivr paths these files live under, so the CSP alone
does not stop a changed file on the CDN from running with this site's
privileges; `integrity` does. A new CDN include without a hash, or a version
bump that kept the old hash, should fail here rather than in a browser.

To get the hash for a new file:
    curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "app"
CDN = re.compile(r"https://(?:cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com)/[^\s\"'`]+")
TAG = re.compile(r"<(?:script|link)\b[^>]*>", re.S)


def _templates():
    return sorted(ROOT.rglob("*.html"))


def _scripts():
    return sorted(p for p in ROOT.rglob("*.js") if not p.name.endswith(".min.js"))


def test_every_cdn_tag_in_a_template_has_integrity():
    missing = []
    for path in _templates():
        for tag in TAG.findall(path.read_text(encoding="utf-8")):
            if 'rel="preconnect"' in tag or not CDN.search(tag):
                continue
            if "integrity=" not in tag or 'crossorigin="anonymous"' not in tag:
                missing.append(f"{path.relative_to(ROOT)}: {' '.join(tag.split())[:120]}")
    assert not missing, "CDN include without integrity/crossorigin:\n" + "\n".join(missing)


def test_every_cdn_url_loaded_from_javascript_has_integrity_beside_it():
    """Dynamically loaded files are declared as {src|href, integrity} pairs."""
    missing = []
    for path in _scripts():
        text = path.read_text(encoding="utf-8")
        for match in CDN.finditer(text):
            following = text[match.end() : match.end() + 200]
            if not re.match(r"['\"],?\s*\n?\s*integrity:\s*['\"]sha384-", following):
                missing.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    assert not missing, "CDN URL in JS without an integrity hash next to it:\n" + "\n".join(
        missing
    )


def test_template_hashes_are_well_formed():
    for path in _templates():
        for value in re.findall(r'integrity="([^"]+)"', path.read_text(encoding="utf-8")):
            assert re.fullmatch(r"sha384-[A-Za-z0-9+/]{64}", value), (path, value)
