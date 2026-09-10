"""Open-access full text: what may be fetched, and what may be kept.

Two separate questions, and conflating them is the mistake this module exists
to prevent.

**May we fetch it?** Yes, when OpenAlex reports a `best_oa_location`. That
location is open by definition, and fetching it is reading.

**May we keep it?** Only when the licence says so. Open access governs
*reading*; redistribution is a separate grant that a licence either makes or
does not. "Bronze" OA is the clearest case: free to read on the publisher's
site, no licence at all, no right to republish. A CC BY paper is the opposite.

So the text is always used, and only sometimes stored. For everything else the
project already has the pattern -- `VideoSummary` keeps an LLM summary and a
character count and drops the transcript (SCRAPING.md §11). Full text follows
it.

No browser automation here, per ADR-0001: a plain GET, and whatever that
returns is what we work with. A PDF that needs JavaScript to assemble is a
paper we skip.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import requests
import structlog

from app.modules.scrape import robots
from app.modules.scrape.fetcher import (
    USER_AGENT,
    fetch_budget,
    get_with_redirects,
    read_capped,
)

log = structlog.get_logger(__name__)

#: Licences that grant redistribution, so the text may be stored and searched.
#:
#: All six Creative Commons variants qualify: every CC licence permits
#: redistribution of the unmodified work with attribution. ND forbids derivative
#: works and NC forbids commercial use -- neither restricts storing and
#: searching a copy, which is what this list gates. Public-domain dedications
#: qualify for the obvious reason.
#:
#: Deliberately absent: "publisher-specific-oa" (terms vary per publisher and
#: OpenAlex does not carry them), and NULL (no stated licence). Both are
#: readable, neither is redistributable, and the default for anything not
#: listed here is "derive and drop".
REDISTRIBUTABLE_LICENSES = frozenset(
    {
        "cc-by",
        "cc-by-sa",
        "cc-by-nc",
        "cc-by-nd",
        "cc-by-nc-sa",
        "cc-by-nc-nd",
        "cc0",
        "public-domain",
    }
)

#: A fetch that returns less than this is treated as a failure rather than a
#: short paper: PDF extractors return a few dozen characters of metadata for
#: scanned images, and landing pages return navigation chrome. Below this there
#: is nothing worth summarising and storing a number would misreport it.
MIN_USEFUL_CHARS = 500

#: Cap on the bytes pulled from one location. Generous for an article, small
#: enough that a mis-typed URL pointing at a dataset cannot exhaust the worker.
MAX_FETCH_BYTES = 12 * 1024 * 1024


class FullTextError(RuntimeError):
    """Fetching or extracting failed in a way the caller should record."""


@dataclass(frozen=True)
class FullText:
    """The result of one fetch.

    `storable` is the licence decision, resolved once here rather than at each
    call site -- a caller that forgets to check it would silently republish.
    """

    text: str
    chars: int
    storable: bool
    license: str | None


def may_store(license_: str | None) -> bool:
    """Whether a licence permits keeping the text, not merely reading it."""
    if not license_:
        return False
    return license_.strip().lower() in REDISTRIBUTABLE_LICENSES


def _extract_pdf(payload: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise FullTextError("pypdf is not installed") from exc

    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:
        raise FullTextError(f"unreadable PDF: {exc}") from exc

    if reader.is_encrypted:
        # Some OA PDFs carry an owner password that permits reading with an
        # empty user password; pypdf needs to be told to try.
        try:
            reader.decrypt("")
        except Exception as exc:
            raise FullTextError(f"encrypted PDF: {exc}") from exc

    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            # One unreadable page should not lose the other forty.
            continue
    return "\n".join(parts)


def _extract_html(payload: bytes) -> str:
    try:
        import trafilatura
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise FullTextError("trafilatura is not installed") from exc

    html = payload.decode("utf-8", errors="replace")
    # `favor_precision` because a false positive here becomes a paragraph of
    # navigation chrome inside an LLM prompt and inside an embedding.
    extracted = trafilatura.extract(html, favor_precision=True, include_comments=False)
    return extracted or ""


def _normalise(text: str) -> str:
    """Collapse the whitespace PDF extraction produces.

    A two-column PDF yields runs of newlines and stray spaces that inflate the
    character count and waste prompt budget without carrying meaning.
    """
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_fulltext(url: str, *, license_: str | None) -> FullText:
    """Fetch one OA location and extract its text.

    Goes through the same gates as any other user-facing fetch in this module,
    for the same reasons: `robots.is_allowed` because §11 of SCRAPING.md makes
    it a contract rather than a courtesy, `robots.host_slot` so a repository
    hosting ten thousand of our OA papers is not hit ten thousand times in a
    row, and `get_with_redirects` so every hop is re-validated against the SSRF
    guard rather than only the first.

    Raises `FullTextError` for anything the caller should record as a failed
    attempt: robots said no, the redirect chain left the allowed hosts, the
    response was neither PDF nor HTML, or extraction produced nothing usable.
    """
    allowed, reason = robots.is_allowed(url)
    if not allowed:
        raise FullTextError(f"robots.txt disallows it: {reason or url}")
    if not robots.host_slot(url):
        raise FullTextError("host rate limit")

    timeout, _budget_bytes, allow_private = fetch_budget()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"}

    try:
        resp, _final_url, hop_status = get_with_redirects(
            url, headers, timeout, allow_private=allow_private
        )
    except requests.RequestException as exc:
        raise FullTextError(f"request failed: {exc}") from exc

    if resp is None:
        raise FullTextError(f"fetch refused: {hop_status}")

    try:
        if resp.status_code != 200:
            raise FullTextError(f"HTTP {resp.status_code}")
        content_type = (resp.headers.get("Content-Type") or "").lower()
        try:
            payload = read_capped(resp, MAX_FETCH_BYTES)
        except requests.RequestException as exc:
            raise FullTextError(f"read failed: {exc}") from exc
    finally:
        resp.close()

    if payload is None:
        raise FullTextError(f"larger than {MAX_FETCH_BYTES} bytes")
    if not payload:
        raise FullTextError("empty response")

    # Sniff as well as trust the header: OA repositories serve PDFs as
    # application/octet-stream often enough that the header alone loses them.
    if "pdf" in content_type or payload[:5] == b"%PDF-":
        text = _extract_pdf(payload)
    elif "html" in content_type or "xml" in content_type:
        text = _extract_html(payload)
    else:
        raise FullTextError(f"unsupported content type: {content_type or 'unknown'}")

    text = _normalise(text)
    if len(text) < MIN_USEFUL_CHARS:
        raise FullTextError(f"extracted only {len(text)} characters")

    return FullText(
        text=text,
        chars=len(text),
        storable=may_store(license_),
        license=license_,
    )
