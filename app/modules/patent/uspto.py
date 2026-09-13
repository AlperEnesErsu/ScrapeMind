"""Finding and fetching USPTO's weekly grant full-text file.

Module-level `requests` on purpose: the tests monkeypatch this module's own
`requests`, so it must not hide behind a shared HTTP wrapper
(`docs/SCRAPING.md` §2). No `net_guard` either — every URL here is derived
from a constant USPTO host, never from user input, so the SSRF surface the
guard exists for is absent.

**An Open Data Portal API key is required.** The first version of this module
treated the key as optional: grants publish on Tuesdays and the weekly filename
is a pure function of the date, so without a key it derived a
`bulkdata.uspto.gov` URL. That route no longer exists, measured on 13 September
2026 from outside any sandbox:

- `bulkdata.uspto.gov` has no address record -- the host is retired;
- since 18 June 2026 the Open Data Portal itself requires signing in with a
  USPTO.gov account (with multi-factor authentication), and its API answers 401
  without a key.

So there is no keyless path left to fall back to, and the module no longer
pretends one exists. Without `USPTO_ODP_API_KEY` the weekly load is skipped
with a stated reason instead of fetching a dead URL every Wednesday.

> NOT YET VALIDATED against the live API. The ODP response shape is inferred
> from its documentation; `_extract_files` tolerates shape differences rather
> than assuming one. The first run with a real key is the gate.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests
import structlog
from flask import current_app

logger = structlog.get_logger()

SOURCE_NAME = "uspto_bulk"

_ODP_PRODUCTS_URL = "https://api.uspto.gov/api/v1/datasets/products/search"

_TIMEOUT = 60  # seconds; these are large files on a slow origin
_CHUNK = 1 << 20  # 1 MiB
#: A weekly grant archive is ~100 MB compressed. The cap is a guard against a
#: redirect to something unexpected, not a real expectation.
_MAX_MB_DEFAULT = 600


@dataclass(frozen=True)
class WeeklyFile:
    """One weekly release, before it has been downloaded."""

    name: str  # "ipg260908.zip"
    url: str
    published: date


def credentials_ok() -> bool:
    """Read per call, not captured at import — same contract as every source
    adapter. Without a key there is no route to the data (see module doc)."""
    return bool(os.getenv("USPTO_ODP_API_KEY") or current_app.config.get("USPTO_ODP_API_KEY"))


def _api_key() -> str:
    return os.getenv("USPTO_ODP_API_KEY") or current_app.config.get("USPTO_ODP_API_KEY", "")


class CredentialsMissingError(RuntimeError):
    """No ODP API key. A configuration state, not a transient failure: retrying
    changes nothing until someone sets the key."""


def _extract_files(payload: object) -> list[dict]:
    """Pull file entries out of an ODP response without assuming its shape.

    The API is versioned and its envelope has changed before; what stays
    stable is that a file entry carries a name and a URL. Walking for that
    pair survives an envelope rename, where indexing a fixed path would not.
    """
    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            name = next(
                (
                    node[k]
                    for k in ("fileName", "fileNameText", "name")
                    if isinstance(node.get(k), str)
                ),
                None,
            )
            url = next(
                (
                    node[k]
                    for k in ("fileDownloadURI", "fileDownloadUri", "downloadUrl", "url")
                    if isinstance(node.get(k), str)
                ),
                None,
            )
            if name and url:
                found.append({"name": name, "url": url})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _parse_weekly_name(name: str) -> date | None:
    """`ipg260908.xml` → 2026-09-08. Anything else is not a weekly grant file."""
    stem = Path(name).stem.lower()
    if not stem.startswith("ipg") or len(stem) < 9:
        return None
    digits = stem[3:9]
    if not digits.isdigit():
        return None
    try:
        return datetime.strptime(digits, "%y%m%d").date()
    except ValueError:
        return None


def discover_latest() -> WeeklyFile:
    """The newest weekly grant file listed by the ODP API.

    Raises `CredentialsMissingError` without a key and lets transport or shape
    errors propagate. The earlier version fell back to a derived legacy URL
    here; that host is gone, so a fallback would only turn a clear failure
    into a confusing 404 one step later.
    """
    if not credentials_ok():
        raise CredentialsMissingError("USPTO_ODP_API_KEY is not set")

    resp = requests.get(
        _ODP_PRODUCTS_URL,
        params={"q": "patent grant full text"},
        headers={"X-API-KEY": _api_key()},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    candidates = [
        WeeklyFile(name=entry["name"], url=entry["url"], published=published)
        for entry in _extract_files(resp.json())
        if (published := _parse_weekly_name(entry["name"])) is not None
    ]
    if not candidates:
        raise ValueError("ODP returned no weekly grant files -- response shape may have changed")
    return max(candidates, key=lambda f: f.published)


def _max_bytes() -> int:
    mb = current_app.config.get("PATENT_MAX_DOWNLOAD_MB", _MAX_MB_DEFAULT)
    try:
        return int(mb) * 1024 * 1024
    except (TypeError, ValueError):
        return _MAX_MB_DEFAULT * 1024 * 1024


def download(weekly: WeeklyFile, dest_dir: str | None = None) -> tuple[str, str]:
    """Fetch one weekly file to disk. Returns (path, sha256).

    Streamed and size-capped: this is the one place in the app that pulls a
    file measured in hundreds of megabytes, and buffering it in memory would
    put a worker at risk for no gain.

    An already-downloaded file is reused rather than re-fetched — a re-run
    after a parse failure should not cost another 100 MB from USPTO.
    """
    directory = Path(dest_dir or current_app.config.get("PATENT_BULK_DIR", "./data/patent_bulk"))
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / weekly.name

    if target.exists() and target.stat().st_size > 0:
        logger.info("uspto_download_reused", file=weekly.name)
        return str(target), _sha256(target)

    if not credentials_ok():
        raise CredentialsMissingError("USPTO_ODP_API_KEY is not set")
    headers = {"X-API-KEY": _api_key()}
    cap = _max_bytes()
    written = 0
    with requests.get(weekly.url, headers=headers, timeout=_TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        partial = target.with_suffix(target.suffix + ".part")
        with open(partial, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                written += len(chunk)
                if written > cap:
                    fh.close()
                    partial.unlink(missing_ok=True)
                    raise ValueError(f"{weekly.name} exceeded {cap // (1024 * 1024)} MB cap")
                fh.write(chunk)
        # Renamed only once complete, so an interrupted download can never be
        # mistaken for a usable file by the reuse branch above.
        partial.replace(target)

    logger.info("uspto_download_done", file=weekly.name, bytes=written)
    return str(target), _sha256(target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_xml(path: str) -> str:
    """USPTO ships a zip containing one XML. Returns a path to the XML.

    Extracted next to the archive and reused on a later run: unzipping ~1 GB
    is not free, and the parser needs a real file to stream rather than a
    zip member handle.
    """
    if not path.lower().endswith(".zip"):
        return path

    archive = Path(path)
    with zipfile.ZipFile(archive) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".xml")]
        if not members:
            raise ValueError(f"{archive.name} contains no XML member")
        member = members[0]
        extracted = archive.parent / Path(member).name
        if extracted.exists() and extracted.stat().st_size > 0:
            return str(extracted)
        with zf.open(member) as src, open(extracted, "wb") as dst:
            while chunk := src.read(_CHUNK):
                dst.write(chunk)
    return str(extracted)
