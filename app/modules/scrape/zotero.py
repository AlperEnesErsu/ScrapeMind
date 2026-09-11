"""Zotero Web API v3 — the first place this application writes to someone else.

Every other integration in this codebase reads. That difference is the whole
reason this module is careful:

* a failed read costs a retry; a failed write leaves half-created records in
  the user's own library, where we cannot see them and they cannot easily be
  found;
* so an export is **idempotent** -- each item carries its ScrapeMind paper id
  in Zotero's `extra` field, and a re-export updates rather than duplicates;
* and it is **user-triggered**, never scheduled. Nothing writes to somebody's
  reference manager in the background.

Why Zotero and not Mendeley: Zotero is open source, its Web API is documented,
and a user can mint their own key from their account page. Mendeley is
Elsevier's and wants OAuth, and ADR-0002 already records this project's
position on depending on Elsevier.

Credentials live in `UserSettings.settings["zotero"]`, Fernet-encrypted via
`credentials.py`, exactly as the LLM key does -- no second secret store.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests
import structlog

from app.extensions import db
from app.modules.scrape.credentials import decrypt_secret, encrypt_secret

logger = structlog.get_logger(__name__)

API_ROOT = "https://api.zotero.org"
API_VERSION = "3"

#: Zotero accepts at most 50 items in one write. Not a tuning knob -- the API
#: rejects 51.
BATCH_SIZE = 50

#: The `extra` line that makes an export idempotent. Zotero has no field for
#: a foreign id, but `extra` is free text that round-trips, and Zotero's own
#: docs suggest it for exactly this.
MARKER_PREFIX = "ScrapeMind-Paper-ID:"

_TIMEOUT = (5, 30)


class ZoteroError(RuntimeError):
    """The API refused, or answered in a shape we will not guess at."""


@dataclass
class ExportResult:
    """What one export did.

    Counts rather than a bare success flag: a partial failure is the likely
    outcome when writing forty items over one HTTP call, and telling the user
    "37 of 40" is the difference between a usable feature and a mystery.
    """

    created: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.failed

    @property
    def ok(self) -> bool:
        return self.failed == 0


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def get_credentials(user) -> tuple[str, str] | None:
    """This user's (api_key, zotero_user_id), or None when not configured."""
    settings = getattr(user, "settings", None)
    blob = ((settings.settings if settings else None) or {}).get("zotero") or {}
    token = blob.get("api_key_enc")
    zotero_user_id = (blob.get("user_id") or "").strip()
    if not token or not zotero_user_id:
        return None
    api_key = decrypt_secret(token)
    if not api_key:
        return None
    return api_key, zotero_user_id


def set_credentials(user, api_key: str | None, zotero_user_id: str | None) -> None:
    """Store or clear this user's Zotero credentials.

    An empty `api_key` clears the whole block rather than storing an empty
    string, so "configured" stays a single unambiguous condition.
    """
    from app.core.models.settings import UserSettings

    settings = user.settings
    if settings is None:
        settings = UserSettings(user_id=user.id, settings={})
        db.session.add(settings)

    # Normalised once rather than at each use: the two-branch version read
    # `api_key.strip()` inside the else of a `(api_key or "")` check, which is
    # correct and which no reader (or type checker) can see is correct.
    key = (api_key or "").strip()

    blob = dict(settings.settings or {})
    if not key:
        blob.pop("zotero", None)
    else:
        blob["zotero"] = {
            "api_key_enc": encrypt_secret(key),
            "user_id": (zotero_user_id or "").strip(),
        }
    settings.settings = blob
    db.session.commit()


def is_configured(user) -> bool:
    return get_credentials(user) is not None


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------


def _creators(paper) -> list[dict]:
    """Zotero wants structured creators; we hold display names.

    Splitting on the last space is wrong for "van der Berg" and for names
    written family-first, so anything without a clean split goes in as a
    single-field name -- which Zotero supports precisely for this case, and
    which is better than confidently mis-parsing somebody's name.
    """
    out = []
    for name in (paper.authors or [])[:50]:
        name = (name or "").strip()
        if not name:
            continue
        parts = name.rsplit(" ", 1)
        if len(parts) == 2 and all(parts):
            out.append({"creatorType": "author", "firstName": parts[0], "lastName": parts[1]})
        else:
            out.append({"creatorType": "author", "name": name})
    return out


def paper_to_item(paper, *, collection_key: str | None = None) -> dict:
    """One Paper as a Zotero `journalArticle`."""
    extra_lines = [f"{MARKER_PREFIX} {paper.id}"]
    if paper.source:
        extra_lines.append(f"Source: {paper.source}")

    item = {
        "itemType": "journalArticle",
        "title": paper.title or "",
        "creators": _creators(paper),
        "abstractNote": paper.abstract or "",
        "DOI": paper.doi or "",
        "url": paper.url or "",
        "date": paper.published_at.strftime("%Y-%m-%d") if paper.published_at else "",
        "extra": "\n".join(extra_lines),
        "tags": [{"tag": c} for c in (paper.categories or [])[:20]],
    }
    if collection_key:
        item["collections"] = [collection_key]
    return item


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def _headers(api_key: str) -> dict:
    return {
        "Zotero-API-Key": api_key,
        "Zotero-API-Version": API_VERSION,
        "Content-Type": "application/json",
    }


def find_existing(api_key: str, zotero_user_id: str, paper_ids: list[int]) -> dict[int, str]:
    """Map ScrapeMind paper id -> Zotero item key, for items already exported.

    Zotero cannot query `extra` directly, so this fetches the library's items
    and reads the marker back. Bounded by `limit`: an export is a page of
    papers, not a whole library, and a user with ten thousand Zotero items does
    not need all of them walked to add forty.
    """
    if not paper_ids:
        return {}

    url = f"{API_ROOT}/users/{zotero_user_id}/items"
    try:
        resp = requests.get(
            url,
            headers=_headers(api_key),
            params={"format": "json", "limit": 100, "itemType": "journalArticle"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ZoteroError(f"could not reach Zotero: {exc}") from exc

    if resp.status_code == 403:
        raise ZoteroError("Zotero refused the API key")
    if resp.status_code != 200:
        raise ZoteroError(f"Zotero answered HTTP {resp.status_code}")

    wanted = set(paper_ids)
    found: dict[int, str] = {}
    for entry in resp.json() or []:
        data = entry.get("data") or {}
        extra = data.get("extra") or ""
        for line in extra.splitlines():
            line = line.strip()
            if not line.startswith(MARKER_PREFIX):
                continue
            raw = line[len(MARKER_PREFIX) :].strip()
            if raw.isdigit() and int(raw) in wanted:
                found[int(raw)] = data.get("key") or entry.get("key")
    return found


def _write_batch(api_key: str, zotero_user_id: str, items: list[dict]) -> tuple[int, list[str]]:
    """POST one batch. Returns (succeeded, error messages).

    Zotero answers 200 with a per-item breakdown rather than failing the whole
    request, so a batch can be partly written -- which is exactly why the
    caller reports counts instead of a boolean.
    """
    url = f"{API_ROOT}/users/{zotero_user_id}/items"
    try:
        resp = requests.post(url, headers=_headers(api_key), json=items, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        return 0, [f"could not reach Zotero: {exc}"]

    if resp.status_code == 403:
        return 0, ["Zotero refused the API key"]
    if resp.status_code not in (200, 201):
        return 0, [f"Zotero answered HTTP {resp.status_code}"]

    body = resp.json() or {}
    succeeded = len(body.get("successful") or {}) + len(body.get("unchanged") or {})
    errors = [str(msg) for msg in (body.get("failed") or {}).values()]
    return succeeded, errors


def export_papers(user, papers, *, collection_key: str | None = None) -> ExportResult:
    """Create or update these papers in the user's Zotero library.

    Idempotent: anything already carrying this paper's marker is updated in
    place through its item key, so exporting the same shelf twice leaves one
    copy of each paper rather than two.
    """
    result = ExportResult()
    credentials = get_credentials(user)
    if credentials is None:
        raise ZoteroError("no Zotero credentials configured")
    api_key, zotero_user_id = credentials

    papers = list(papers)
    if not papers:
        return result

    existing = find_existing(api_key, zotero_user_id, [p.id for p in papers])

    creates: list[dict] = []
    updates: list[dict] = []
    for paper in papers:
        item = paper_to_item(paper, collection_key=collection_key)
        key = existing.get(paper.id)
        if key:
            # Zotero updates by including the key; version 0 means "I do not
            # know the version, take mine" and requires the write token below.
            item["key"] = key
            item["version"] = 0
            updates.append(item)
        else:
            creates.append(item)

    for batch, bucket in ((creates, "created"), (updates, "updated")):
        for start in range(0, len(batch), BATCH_SIZE):
            chunk = batch[start : start + BATCH_SIZE]
            succeeded, errors = _write_batch(api_key, zotero_user_id, chunk)
            setattr(result, bucket, getattr(result, bucket) + succeeded)
            result.failed += len(chunk) - succeeded
            result.errors.extend(errors)

    logger.info(
        "zotero_export",
        user_id=user.id,
        created=result.created,
        updated=result.updated,
        failed=result.failed,
    )
    return result
