"""normalize_doi — pure function, table-driven.

No DB/Flask context needed; `service.upsert_paper`'s use of this is covered
separately in test_scrape.py.
"""

from __future__ import annotations

import pytest

from app.modules.scrape.doi import normalize_doi

_CASES = [
    ("bare", "10.1234/abc", "10.1234/abc"),
    ("https doi.org uppercase suffix lowercased", "https://doi.org/10.1234/ABC", "10.1234/abc"),
    ("doi scheme prefix", "doi:10.1234/abc", "10.1234/abc"),
    ("http dx.doi.org prefix", "http://dx.doi.org/10.1234/abc", "10.1234/abc"),
    ("https dx.doi.org prefix", "https://dx.doi.org/10.1234/abc", "10.1234/abc"),
    ("http doi.org prefix", "http://doi.org/10.1234/abc", "10.1234/abc"),
    ("doi scheme case-insensitive", "DOI:10.1234/abc", "10.1234/abc"),
    ("surrounding whitespace", "  10.1234/abc  ", "10.1234/abc"),
    ("trailing period", "10.1234/abc.", "10.1234/abc"),
    ("trailing comma", "10.1234/abc,", "10.1234/abc"),
    ("none", None, None),
    ("empty string", "", None),
    ("blank string", "   ", None),
    ("not a doi", "not a doi", None),
    ("too few registrant digits", "10.1/x", None),
    ("nine registrant digits still valid", "10.123456789/x", "10.123456789/x"),
    ("ten registrant digits rejected", "10.1234567890/x", None),
    ("non-string input", 12345, None),
]


@pytest.mark.parametrize(
    ("label", "raw", "expected"),
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_normalize_doi(label, raw, expected):  # noqa: ARG001 — label only used for the test id
    assert normalize_doi(raw) == expected
