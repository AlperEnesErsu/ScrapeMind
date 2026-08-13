"""Journal quality columns and the refresh-vs-fill enrichment split (Faz 5.3).

The load-bearing distinction here: `cited_by_count` is a running total and
must be *refreshed*, while everything else stays fill-only. Getting that
backwards shows a 2019 paper's first-ever citation count forever — worse than
showing nothing, because it looks precise.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.modules.scrape.models import Journal, Paper
from app.modules.scrape.service import upsert_paper
from app.modules.scrape.sources.payload import PaperPayload

#: A DOI that actually passes `doi.normalize_doi` (`10.\d{4,9}/…`). The
#: enrichment tests below rely on two payloads from different sources landing
#: on the same row via the DOI branch of `upsert_paper`.
_DOI = "10.1234/abc"


def _payload(ext_id=_DOI, **overrides):
    base = {
        "source": "openalex",
        "external_id": ext_id,
        "title": "A Paper",
        "abstract": "abs",
        "authors": ["A. One"],
        "url": "https://example.test/a",
        "pdf_url": None,
        "published_at": datetime(2019, 5, 1, tzinfo=UTC),
        "categories": ["cs"],
        "doi": _DOI,
    }
    base.update(overrides)
    return PaperPayload(**base)


@pytest.fixture(autouse=True)
def clean_papers(db):
    yield
    db.session.rollback()
    db.session.execute(text("DELETE FROM user_papers"))
    db.session.execute(text("DELETE FROM papers"))
    db.session.execute(text("DELETE FROM journals"))
    db.session.commit()


# ----------------------------------------------------------------------------
# Payload / column plumbing
# ----------------------------------------------------------------------------


def test_payload_defaults_keep_existing_adapters_working():
    """Both fields default to None so an adapter that never heard of them
    stays valid — same precedent as `kind`."""
    p = PaperPayload(
        source="arxiv",
        external_id="1",
        title="t",
        abstract=None,
        authors=[],
        url=None,
        pdf_url=None,
        published_at=None,
        categories=[],
    )
    assert p.issn_l is None
    assert p.cited_by_count is None
    assert p.as_dict()["cited_by_count"] is None


def test_upsert_persists_the_quality_fields(app, db):
    paper = upsert_paper(_payload(issn_l="1234-567X", cited_by_count=12))
    assert paper.issn_l == "1234-567X"
    assert paper.cited_by_count == 12


# ----------------------------------------------------------------------------
# Refresh vs fill
# ----------------------------------------------------------------------------


def test_citation_count_is_refreshed_not_frozen(app, db):
    """The whole reason `_REFRESHABLE_FIELDS` exists."""
    upsert_paper(_payload(cited_by_count=12))
    paper = upsert_paper(_payload(cited_by_count=300))
    assert paper.cited_by_count == 300


def test_citation_count_can_go_down(app, db):
    """Sources disagree and corrections happen; "latest wins" is the rule, not
    "highest wins"."""
    upsert_paper(_payload(cited_by_count=300))
    paper = upsert_paper(_payload(source="crossref", external_id="x", cited_by_count=280))
    assert paper.cited_by_count == 280


def test_a_source_that_omits_citations_does_not_wipe_them(app, db):
    """None means "this source didn't say", not "zero citations"."""
    upsert_paper(_payload(cited_by_count=42))
    paper = upsert_paper(_payload(source="arxiv", external_id="arx1", cited_by_count=None))
    assert paper.cited_by_count == 42


def test_zero_citations_is_a_real_value_and_overwrites(app, db):
    """0 is not empty — a paper genuinely reported as uncited must be able to
    replace a stale number."""
    upsert_paper(_payload(cited_by_count=5))
    paper = upsert_paper(_payload(cited_by_count=0))
    assert paper.cited_by_count == 0


def test_issn_is_fill_only(app, db):
    """A paper's journal does not change, so the first non-empty value wins —
    unlike the citation count next to it."""
    upsert_paper(_payload(issn_l="1234-567X"))
    paper = upsert_paper(_payload(source="crossref", external_id="c1", issn_l="9999-9999"))
    assert paper.issn_l == "1234-567X"


def test_issn_backfills_an_empty_row(app, db):
    upsert_paper(_payload(issn_l=None))
    paper = upsert_paper(_payload(source="crossref", external_id="c1", issn_l="1234-567X"))
    assert paper.issn_l == "1234-567X"


def test_abstract_still_never_overwritten(app, db):
    """Guard against the refresh rule leaking into the fill-only fields."""
    upsert_paper(_payload(abstract="first"))
    paper = upsert_paper(_payload(source="crossref", external_id="c1", abstract="second"))
    assert paper.abstract == "first"


def test_enrichment_reaches_a_row_created_by_another_source(app, db):
    """arXiv creates the row with no citations; OpenAlex enriches it through
    the DOI match — which is why only two adapters need to report these."""
    upsert_paper(_payload(source="arxiv", external_id="2401.1", cited_by_count=None, issn_l=None))
    paper = upsert_paper(
        _payload(source="openalex", external_id="W1", cited_by_count=7, issn_l="1234-567X")
    )
    assert Paper.query.count() == 1
    assert paper.cited_by_count == 7
    assert paper.issn_l == "1234-567X"


# ----------------------------------------------------------------------------
# Journal model
# ----------------------------------------------------------------------------


def test_journal_round_trips(app, db):
    j = Journal(
        issn_l="1234-567X",
        title="Journal of Examples",
        publisher="Example Press",
        sjr="3.125",
        sjr_quartile="Q1",
        sjr_year=2025,
        h_index=120,
        is_doaj=True,
        is_oa=True,
    )
    db.session.add(j)
    db.session.commit()

    row = Journal.query.filter_by(issn_l="1234-567X").one()
    assert row.sjr_quartile == "Q1"
    assert float(row.sjr) == 3.125  # Numeric, not float — exact decimals
    assert row.sjr_year == 2025


def test_issn_is_unique(app, db):
    from sqlalchemy.exc import IntegrityError

    db.session.add(Journal(issn_l="1111-1111", title="One"))
    db.session.commit()
    db.session.add(Journal(issn_l="1111-1111", title="Duplicate"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_boolean_flags_default_to_false(app, db):
    db.session.add(Journal(issn_l="2222-2222", title="Minimal"))
    db.session.commit()

    row = Journal.query.filter_by(issn_l="2222-2222").one()
    assert row.is_doaj is False
    assert row.is_oa is False
