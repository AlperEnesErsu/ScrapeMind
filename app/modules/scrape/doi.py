"""DOI normalization.

`Paper` dedup used to rely solely on `(source, external_id)` — every source
mints its own id scheme, so the same work ingested from two academic sources
(arXiv + Semantic Scholar, say) landed as two unrelated rows. A DOI is the
one identifier several of those sources actually agree on, but only if it is
reduced to a single canonical form first: "10.1000/182", "doi:10.1000/182",
"https://doi.org/10.1000/182" and "https://dx.doi.org/10.1000/182" are all
the same DOI as far as a human is concerned, but four different strings as
far as an exact `WHERE doi = ...` lookup is concerned.

`normalize_doi` is deliberately conservative: anything that doesn't clearly
look like a DOI after stripping the common prefixes returns `None` rather
than a best-effort guess. A wrong normalization silently merges two
different papers into one `Paper` row — worse than leaving the DOI blank and
falling back to `(source, external_id)`.
"""

from __future__ import annotations

import re

#: doi.org / dx.doi.org resolver prefixes, plus the bare "doi:" scheme some
#: metadata feeds use. Order doesn't matter since we match once, case-
#: insensitively, against the start of the (already-stripped) string.
_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)

#: A DOI is "10.<4-9 digit registrant code>/<suffix>". The suffix is
#: intentionally unconstrained (`\S+`) — registrants mint arbitrary suffixes
#: and being stricter here would reject real DOIs.
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def normalize_doi(value: str | None) -> str | None:
    """Return `value` reduced to a canonical lowercase DOI, or `None` if it
    isn't recognizably one.

    None/non-str/blank input, and input that still doesn't match the DOI
    shape after prefix-stripping, both return None — callers should treat
    that as "no DOI", not as an error.
    """
    if not value or not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None

    lowered = candidate.lower()
    for prefix in _PREFIXES:
        if lowered.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break

    # Real-world metadata (esp. Crossref/PubMed exports) often trails a DOI
    # with sentence punctuation picked up from the surrounding citation text.
    candidate = candidate.strip().rstrip(".,")
    candidate = candidate.lower()

    if not _DOI_RE.match(candidate):
        return None
    return candidate
