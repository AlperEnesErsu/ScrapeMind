"""USPTO weekly grant full-text XML → plain Python objects.

Pure parsing: no Flask, no database, no network. That keeps it testable from a
fixture and keeps the ingest task free to decide what to do with the result.

**Two things about the file format drive this whole module.**

1. A weekly grant file (`ipgYYMMDD.xml`) is *not* one XML document. It is
   thousands of complete documents concatenated, each with its own `<?xml?>`
   declaration and DOCTYPE. Handing the whole file to any XML parser fails on
   the second declaration, so `iter_documents` splits on the declaration and
   yields one document at a time — which also keeps memory flat regardless of
   the file being ~1 GB uncompressed.

2. Claim dependency lives in markup, not an attribute. A dependent claim
   contains a `<claim-ref idref="CLM-00003">`; an independent one does not.
   That is the only reliable signal, so `is_independent` is derived from it
   rather than from wording like "The method of claim 3".

> NOT YET VALIDATED: written against USPTO's documented grant DTD (v4.x), but not
> against a real weekly file — the sandbox that produced it had no route to
> `bulkdata.uspto.gov`. Phase 8.3 must run it over one real file before the
> pipeline is trusted; expect field-level surprises, not structural ones.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from xml.etree import ElementTree as ET

# The declaration that starts every embedded document. Anchored to the line
# start because the string can legitimately occur inside a description.
_XML_DECL = re.compile(rb"^<\?xml\s", re.MULTILINE)

# "CLM-00003" → 3
_CLAIM_REF = re.compile(r"CLM-0*(\d+)")


@dataclass
class ParsedClaim:
    number: int
    text: str
    is_independent: bool
    depends_on: int | None = None


@dataclass
class ParsedPatent:
    doc_number: str
    kind_code: str | None
    country: str
    title: str
    abstract: str | None
    description: str | None
    filing_date: date | None
    grant_date: date | None
    priority_date: date | None
    assignees: list[str] = field(default_factory=list)
    inventors: list[str] = field(default_factory=list)
    cpc_codes: list[str] = field(default_factory=list)
    claims: list[ParsedClaim] = field(default_factory=list)
    raw_sha256: str = ""

    @property
    def claim_count(self) -> int:
        return len(self.claims)

    @property
    def claim_one(self) -> ParsedClaim | None:
        """The claim that defines scope. Almost always number 1, but a
        reissue can renumber, so fall back to the first independent claim."""
        for claim in self.claims:
            if claim.number == 1:
                return claim
        for claim in self.claims:
            if claim.is_independent:
                return claim
        return None


def iter_documents(path: str, *, encoding: str = "utf-8") -> Iterator[bytes]:
    """Yield each embedded XML document from a weekly grant file.

    Reads in chunks and splits on the XML declaration, so peak memory is one
    patent document rather than the whole file.
    """
    buffer = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            buffer += chunk
            parts = _XML_DECL.split(buffer)
            # The last part may be a partial document — keep it buffered. Note
            # split() drops the delimiter, so it is put back below.
            buffer = parts.pop() if parts else b""
            for part in parts:
                if part.strip():
                    yield b"<?xml " + part
        if buffer.strip():
            yield b"<?xml " + buffer


def _text(element: ET.Element | None) -> str:
    """All descendant text, whitespace-collapsed.

    Patent markup interleaves text with inline tags (`<i>`, `<sub>`,
    `<claim-ref>`), so `.text` alone loses most of a claim.
    """
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _parse_date(raw: str | None) -> date | None:
    """USPTO dates are `YYYYMMDD`. Day 00 appears for month-only precision."""
    if not raw:
        return None
    raw = raw.strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    year, month, day = int(raw[:4]), int(raw[4:6]), int(raw[6:])
    if not (1 <= month <= 12):
        return None
    try:
        return date(year, month, max(day, 1))
    except ValueError:
        return None


def _cpc_code(node: ET.Element) -> str | None:
    """Assemble `G06N3/08` from the split fields CPC is stored in."""
    section = _text(node.find("section"))
    cls = _text(node.find("class"))
    subclass = _text(node.find("subclass"))
    main_group = _text(node.find("main-group"))
    subgroup = _text(node.find("subgroup"))
    if not (section and cls and subclass):
        return None
    code = f"{section}{cls}{subclass}"
    if main_group:
        code += main_group
        if subgroup:
            code += f"/{subgroup}"
    return code


def _collect_cpc(root: ET.Element) -> list[str]:
    codes: list[str] = []
    for container in root.iter("classifications-cpc"):
        for node in container.iter("classification-cpc"):
            code = _cpc_code(node)
            if code and code not in codes:
                codes.append(code)
    # Older grants carry CPC as a flat text element instead of split fields.
    for node in root.iter("classification-cpc-text"):
        code = _text(node).replace(" ", "")
        if code and code not in codes:
            codes.append(code)
    return codes


def _collect_assignees(root: ET.Element) -> list[str]:
    out: list[str] = []
    for node in root.iter("assignee"):
        name = _text(node.find("./addressbook/orgname"))
        if not name:
            # An individual assignee has a person's name instead of an org.
            last = _text(node.find("./addressbook/last-name"))
            first = _text(node.find("./addressbook/first-name"))
            name = f"{first} {last}".strip()
        if name and name not in out:
            out.append(name)
    return out


def _collect_inventors(root: ET.Element) -> list[str]:
    out: list[str] = []
    for container in root.iter("inventors"):
        for node in container.iter("inventor"):
            last = _text(node.find("./addressbook/last-name"))
            first = _text(node.find("./addressbook/first-name"))
            name = f"{first} {last}".strip()
            if name and name not in out:
                out.append(name)
    return out


def _claim_number(node: ET.Element, fallback: int) -> int:
    """`num` attribute if present, else the id (`CLM-00007`), else position."""
    raw = (node.get("num") or "").strip()
    if raw.isdigit():
        return int(raw)
    match = _CLAIM_REF.search(node.get("id") or "")
    if match:
        return int(match.group(1))
    return fallback


def _collect_claims(root: ET.Element) -> list[ParsedClaim]:
    claims: list[ParsedClaim] = []
    for container in root.iter("claims"):
        for position, node in enumerate(container.iter("claim"), start=1):
            number = _claim_number(node, position)
            # A claim-ref anywhere inside means this claim narrows another.
            ref = node.find(".//claim-ref")
            depends_on = None
            if ref is not None:
                match = _CLAIM_REF.search(ref.get("idref") or "")
                if match:
                    depends_on = int(match.group(1))
            text = _text(node)
            if not text:
                continue
            claims.append(
                ParsedClaim(
                    number=number,
                    text=text,
                    is_independent=ref is None,
                    depends_on=depends_on,
                )
            )
    claims.sort(key=lambda c: c.number)
    return claims


def parse_document(raw: bytes) -> ParsedPatent | None:
    """One embedded document → `ParsedPatent`, or None if unusable.

    A single malformed record is skipped rather than fatal — the same rule the
    source adapters follow (`docs/SCRAPING.md` §2). One bad patent must not
    cost the other 6,000 in the file.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    pub = root.find(".//publication-reference/document-id")
    if pub is None:
        return None
    number = _text(pub.find("doc-number"))
    if not number:
        return None
    country = _text(pub.find("country")) or "US"
    kind = _text(pub.find("kind")) or None

    title = _text(root.find(".//invention-title"))
    if not title:
        return None

    app_ref = root.find(".//application-reference/document-id")
    # Earliest priority date across the whole claim of priority, not the first
    # listed — the order in the XML is not guaranteed to be chronological.
    priority_dates = [
        parsed
        for node in root.iter("priority-claim")
        if (parsed := _parse_date(_text(node.find("date")))) is not None
    ]

    abstract = _text(root.find(".//abstract")) or None
    description = _text(root.find(".//description")) or None

    return ParsedPatent(
        doc_number=f"{country}{number}{kind or ''}",
        kind_code=kind,
        country=country,
        title=title,
        abstract=abstract,
        description=description,
        filing_date=_parse_date(_text(app_ref.find("date"))) if app_ref is not None else None,
        grant_date=_parse_date(_text(pub.find("date"))),
        priority_date=min(priority_dates) if priority_dates else None,
        assignees=_collect_assignees(root),
        inventors=_collect_inventors(root),
        cpc_codes=_collect_cpc(root),
        claims=_collect_claims(root),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


def iter_patents(path: str) -> Iterator[ParsedPatent]:
    """Stream a weekly file as `ParsedPatent`s, skipping unusable records."""
    for raw in iter_documents(path):
        parsed = parse_document(raw)
        if parsed is not None:
            yield parsed
