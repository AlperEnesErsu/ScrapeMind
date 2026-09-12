"""USPTO grant XML parser + CPC classifier (Faz 8.1).

Fixture inline, written to tmp_path as a concatenated weekly file. No network,
no database — the parser is pure by design.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.modules.patent import classify as cls
from app.modules.patent import parser

_AI_PATENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE us-patent-grant SYSTEM "us-patent-grant-v45-2014-04-03.dtd">
<us-patent-grant lang="EN" country="US">
<us-bibliographic-data-grant>
<publication-reference><document-id>
<country>US</country><doc-number>11123456</doc-number><kind>B2</kind><date>20260908</date>
</document-id></publication-reference>
<application-reference appl-type="utility"><document-id>
<country>US</country><doc-number>16987654</doc-number><date>20230214</date>
</document-id></application-reference>
<invention-title id="d2e53">Method for training a neural network</invention-title>
<classifications-cpc>
<main-cpc><classification-cpc>
<section>G</section><class>06</class><subclass>N</subclass>
<main-group>3</main-group><subgroup>08</subgroup>
</classification-cpc></main-cpc>
<further-cpc><classification-cpc>
<section>G</section><class>06</class><subclass>V</subclass>
<main-group>10</main-group><subgroup>82</subgroup>
</classification-cpc></further-cpc>
</classifications-cpc>
<priority-claims>
<priority-claim sequence="01" kind="national"><date>20220301</date></priority-claim>
<priority-claim sequence="02" kind="national"><date>20220115</date></priority-claim>
</priority-claims>
<us-parties>
<inventors>
<inventor sequence="01"><addressbook>
<last-name>Smith</last-name><first-name>Jane</first-name>
</addressbook></inventor>
</inventors>
</us-parties>
<assignees><assignee><addressbook>
<orgname>Example Robotics Inc.</orgname>
</addressbook></assignee></assignees>
</us-bibliographic-data-grant>
<abstract id="abstract"><p id="p-0001">A computer-implemented method of training a
neural network using gradient descent.</p></abstract>
<description id="description">
<p id="p-0002">Machine learning systems require large datasets.</p>
<p id="p-0003">The invention improves convergence.</p>
</description>
<claims id="claims">
<claim id="CLM-00001" num="00001"><claim-text>1. A method comprising: receiving
training data.</claim-text></claim>
<claim id="CLM-00002" num="00002"><claim-text>2. The method of
<claim-ref idref="CLM-00001">claim 1</claim-ref>, wherein the data is
images.</claim-text></claim>
<claim id="CLM-00003" num="00003"><claim-text>3. A system configured to perform
operations.</claim-text></claim>
</claims>
</us-patent-grant>
"""

_NON_AI_PATENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE us-patent-grant SYSTEM "us-patent-grant-v45-2014-04-03.dtd">
<us-patent-grant lang="EN" country="US">
<us-bibliographic-data-grant>
<publication-reference><document-id>
<country>US</country><doc-number>11222333</doc-number><kind>B1</kind><date>20260908</date>
</document-id></publication-reference>
<invention-title>Apparatus for measuring soil density</invention-title>
<classifications-cpc>
<main-cpc><classification-cpc>
<section>G</section><class>01</class><subclass>N</subclass>
<main-group>9</main-group><subgroup>24</subgroup>
</classification-cpc></main-cpc>
</classifications-cpc>
</us-bibliographic-data-grant>
<abstract><p>A probe for measuring density.</p></abstract>
<claims><claim id="CLM-00001" num="00001"><claim-text>1. A probe.</claim-text></claim></claims>
</us-patent-grant>
"""

# Truncated mid-element: must be skipped, not crash the run.
_MALFORMED = """<?xml version="1.0" encoding="UTF-8"?>
<us-patent-grant lang="EN" country="US">
<us-bibliographic-data-grant><publication-reference>
"""


@pytest.fixture
def weekly_file(tmp_path):
    """The three documents concatenated, exactly as USPTO ships a week."""
    path = tmp_path / "ipg260908.xml"
    path.write_text(_AI_PATENT + _NON_AI_PATENT + _MALFORMED, encoding="utf-8")
    return str(path)


def test_iter_documents_splits_on_each_declaration(weekly_file):
    docs = list(parser.iter_documents(weekly_file))
    assert len(docs) == 3
    assert all(d.startswith(b"<?xml ") for d in docs)


def test_malformed_document_is_skipped_not_fatal(weekly_file):
    patents = list(parser.iter_patents(weekly_file))
    assert len(patents) == 2


def test_bibliographic_fields(weekly_file):
    ai = next(iter(parser.iter_patents(weekly_file)))
    assert ai.doc_number == "US11123456B2"
    assert ai.kind_code == "B2"
    assert ai.title == "Method for training a neural network"
    assert ai.grant_date == date(2026, 9, 8)
    assert ai.filing_date == date(2023, 2, 14)
    assert ai.assignees == ["Example Robotics Inc."]
    assert ai.inventors == ["Jane Smith"]


def test_priority_date_is_the_earliest_not_the_first_listed(weekly_file):
    ai = next(iter(parser.iter_patents(weekly_file)))
    assert ai.priority_date == date(2022, 1, 15)


def test_cpc_codes_are_assembled_from_split_fields(weekly_file):
    ai = next(iter(parser.iter_patents(weekly_file)))
    assert ai.cpc_codes == ["G06N3/08", "G06V10/82"]


def test_text_survives_inline_markup(weekly_file):
    ai = next(iter(parser.iter_patents(weekly_file)))
    assert "gradient descent" in ai.abstract
    assert "convergence" in ai.description
    # The dependent claim's text is interrupted by <claim-ref>; itertext keeps it.
    dependent = next(c for c in ai.claims if c.number == 2)
    assert "wherein the data is images" in dependent.text


def test_claim_dependency_comes_from_claim_ref(weekly_file):
    ai = next(iter(parser.iter_patents(weekly_file)))
    assert ai.claim_count == 3
    by_number = {c.number: c for c in ai.claims}
    assert by_number[1].is_independent and by_number[1].depends_on is None
    assert not by_number[2].is_independent and by_number[2].depends_on == 1
    assert by_number[3].is_independent


def test_claim_one_is_the_scope_claim(weekly_file):
    ai = next(iter(parser.iter_patents(weekly_file)))
    assert ai.claim_one.number == 1


def test_sha256_is_stable_per_document(weekly_file):
    first = next(iter(parser.iter_patents(weekly_file)))
    second = next(iter(parser.iter_patents(weekly_file)))
    assert first.raw_sha256 == second.raw_sha256
    assert len(first.raw_sha256) == 64


class TestClassify:
    def test_core_beats_extended(self):
        assert cls.classify(["G06V10/82", "G06N3/08"], extended=("G06V",)) == "cpc_core"

    def test_extended_only_when_enabled(self):
        assert cls.classify(["G06V10/82"]) is None
        assert cls.classify(["G06V10/82"], extended=("G06V",)) == "cpc_extended"

    def test_prefix_match_covers_the_subtree(self):
        assert cls.classify(["G06N20/00"]) == "cpc_core"

    def test_unrelated_patent_is_out(self):
        assert cls.classify(["G01N9/24"]) is None

    def test_empty_is_out(self):
        assert cls.classify([]) is None
        assert cls.classify(None) is None

    def test_whitespace_and_case_are_normalised(self):
        assert cls.classify([" g06n 3/08 "]) == "cpc_core"


def test_fixture_split_matches_classifier(weekly_file):
    """The two survivors are one AI patent and one that must be filtered out."""
    kept = [p for p in parser.iter_patents(weekly_file) if cls.classify(p.cpc_codes)]
    assert [p.doc_number for p in kept] == ["US11123456B2"]
