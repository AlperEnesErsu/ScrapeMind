"""Finding and fetching USPTO's weekly grant full-text file.

Module-level `requests` on purpose: the tests monkeypatch this module's own
`requests`, so it must not hide behind a shared HTTP wrapper
(`docs/SCRAPING.md` §2). No `net_guard` either — every URL here is derived
from a constant USPTO host, never from user input, so the SSRF surface the
guard exists for is absent.

**Two routes to the same file, in order:**

1. The Open Data Portal product API, when `USPTO_ODP_API_KEY` is set. USPTO
   has consolidated its bulk data there and `bulkdata.uspto.gov` is the older
   surface.
2. The legacy weekly path, when there is no key. Grants publish on Tuesdays
   and the filename is a pure function of that date, so the URL can be
   derived rather than discovered — which is what keeps this module usable on
   a deployment that never obtained a key.

> NOT YET VALIDATED. The ODP response shape below is inferred from its
> documentation, and no request in this module has run against the real
> service -- the environment it was written in could not resolve
> `bulkdata.uspto.gov` and had no API key. `_extract_files` is therefore
> written to tolerate shape differences rather than to assume one, and the
> first real run is the gate on Phase 8.3 being called done.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import requests
import structlog
from flask import current_app

logger = structlog.get_logger()

SOURCE_NAME = "uspto_bulk"

_ODP_PRODUCTS_URL = "https://api.uspto.gov/api/v1/datasets/products/search"
#: Grant full text (the "red book"). Applications live under `application/`.
_LEGACY_BASE = "https://bulkdata.uspto.gov/data/patent/grant/redbook/fulltext"

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
    adapter. Absence is not an error here: it selects the legacy route."""
    return bool(os.getenv("USPTO_ODP_API_KEY") or current_app.config.get("USPTO_ODP_API_KEY"))


def _api_key() -> str:
    return os.getenv("USPTO_ODP_API_KEY") or current_app.config.get("USPTO_ODP_API_KEY", "")


def most_recent_tuesday(today: date | None = None) -> date:
    """USPTO grants publish on Tuesdays.

    If today *is* Tuesday the file may not be up yet, so the previous Tuesday
    is returned — a week-old file that exists beats a URL that 404s, and the
    window is three weeks wide either way.
    """
    today = today or datetime.now(UTC).date()
    days_since_tuesday = (today.weekday() - 1) % 7
    if days_since_tuesday == 0:
        days_since_tuesday = 7
    return date.fromordinal(today.toordinal() - days_since_tuesday)


def legacy_weekly_file(published: date | None = None) -> WeeklyFile:
    """The filename is `ipgYYMMDD.zip` — a pure function of the date, which is
    why no key is needed to find it."""
    published = published or most_recent_tuesday()
    name = f"ipg{published:%y%m%d}.zip"
    return WeeklyFile(
        name=name,
        url=f"{_LEGACY_BASE}/{published.year}/{name}",
        published=published,
    )


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


def discover_latest(*, today: date | None = None) -> WeeklyFile:
    """The newest weekly grant file, by whichever route is available.

    The legacy route is the fallback rather than an error path: a deployment
    with no key still tracks patents, it just derives the URL instead of
    asking for it.
    """
    if not credentials_ok():
        return legacy_weekly_file(most_recent_tuesday(today))

    try:
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
    except (requests.RequestException, ValueError) as exc:
        # A key that is present but not working must not take the feature
        # down when a derivable URL exists.
        logger.warning("uspto_odp_discovery_failed", error=str(exc))
        return legacy_weekly_file(most_recent_tuesday(today))

    if not candidates:
        logger.warning("uspto_odp_returned_no_weekly_files")
        return legacy_weekly_file(most_recent_tuesday(today))

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

    headers = {"X-API-KEY": _api_key()} if credentials_ok() else {}
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
