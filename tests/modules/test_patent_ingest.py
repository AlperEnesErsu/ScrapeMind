"""Weekly USPTO load: discovery, download, ingest, purge (Faz 8.3).

No network. The module's own `requests` is monkeypatched, matching every
other adapter in this project (`docs/SCRAPING.md` §2).

The load-bearing properties: the CPC filter runs *before* the write so a
weekly file's ~95% non-AI bulk never reaches the database, an unchanged
document is not rewritten, and the purge cannot take a user's library row
with it.
"""

from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime

import pytest

from app.extensions import db
from app.modules.patent import ingest, uspto
from app.modules.patent.models import PatentClaim, PatentDocument, PatentIngestRun
from app.modules.scrape.models import Paper

_AI = """<?xml version="1.0" encoding="UTF-8"?>
<us-patent-grant lang="EN" country="US">
<us-bibliographic-data-grant>
<publication-reference><document-id>
<country>US</country><doc-number>11123456</doc-number><kind>B2</kind><date>20260908</date>
</document-id></publication-reference>
<invention-title>Training a neural network</invention-title>
<classifications-cpc><main-cpc><classification-cpc>
<section>G</section><class>06</class><subclass>N</subclass>
<main-group>3</main-group><subgroup>08</subgroup>
</classification-cpc></main-cpc></classifications-cpc>
</us-bibliographic-data-grant>
<abstract><p>A method of training a neural network.</p></abstract>
<description><p>Machine learning systems require large datasets.</p></description>
<claims>
<claim id="CLM-00001" num="00001"><claim-text>1. A method comprising receiving data.</claim-text></claim>
<claim id="CLM-00002" num="00002"><claim-text>2. The method of
<claim-ref idref="CLM-00001">claim 1</claim-ref>, wherein data is images.</claim-text></claim>
</claims>
</us-patent-grant>
"""

_VISION_ONLY = _AI.replace("11123456", "11222333").replace(
    "<subclass>N</subclass>", "<subclass>V</subclass>"
)

_NON_AI = _AI.replace("11123456", "11444555").replace(
    "<section>G</section><class>06</class><subclass>N</subclass>",
    "<section>G</section><class>01</class><subclass>N</subclass>",
)


@pytest.fixture
def weekly_xml(tmp_path):
    path = tmp_path / "ipg260908.xml"
    path.write_text(_AI + _VISION_ONLY + _NON_AI, encoding="utf-8")
    return str(path)


#: `app` is session-scoped, so a config value set by one test survives into
#: the next one and makes the suite order-dependent -- the exact failure mode
#: HANDOVER.md section 5.5 records for config-dependent tests. Saving and
#: restoring is what keeps these tests honest when run in any order.
_CONFIG_KEYS = (
    "PATENT_AI_CPC_CODES",
    "PATENT_AI_CPC_EXTENDED",
    "PATENT_WINDOW_WEEKS",
    "PATENT_MAX_DOWNLOAD_MB",
    "USPTO_ODP_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_corpus(app, db):
    saved = {k: app.config.get(k) for k in _CONFIG_KEYS}
    app.config["PATENT_AI_CPC_CODES"] = "G06N"
    app.config["PATENT_AI_CPC_EXTENDED"] = ""
    PatentDocument.query.delete()
    PatentIngestRun.query.delete()
    db.session.commit()
    yield
    app.config.update(saved)
    PatentDocument.query.delete()
    PatentIngestRun.query.delete()
    db.session.commit()


class _Resp:
    def __init__(self, payload=None, chunks=None, status=200):
        self._payload = payload
        self._chunks = chunks or []
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise uspto.requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size=None):  # noqa: ARG002
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDiscovery:
    @pytest.mark.parametrize(
        "today,expected",
        [
            (date(2026, 9, 10), date(2026, 9, 8)),  # Thursday -> this week
            (date(2026, 9, 13), date(2026, 9, 8)),  # Sunday   -> this week
            (date(2026, 9, 14), date(2026, 9, 8)),  # Monday   -> last Tuesday
        ],
    )
    def test_most_recent_tuesday(self, today, expected):
        assert uspto.most_recent_tuesday(today) == expected

    def test_on_a_tuesday_it_takes_the_previous_one(self):
        """Today's file may not be published yet; a week-old file that exists
        beats a URL that 404s."""
        assert uspto.most_recent_tuesday(date(2026, 9, 8)) == date(2026, 9, 1)

    def test_legacy_url_is_derived_not_discovered(self):
        weekly = uspto.legacy_weekly_file(date(2026, 9, 8))
        assert weekly.name == "ipg260908.zip"
        assert weekly.url.endswith("/2026/ipg260908.zip")

    def test_without_a_key_the_legacy_route_is_used(self, app, monkeypatch):
        def explode(*a, **k):
            raise AssertionError("no HTTP call should happen without a key")

        monkeypatch.setattr(uspto.requests, "get", explode)
        with app.app_context():
            app.config["USPTO_ODP_API_KEY"] = ""
            monkeypatch.delenv("USPTO_ODP_API_KEY", raising=False)
            weekly = uspto.discover_latest(today=date(2026, 9, 10))
        assert weekly.name == "ipg260908.zip"

    def test_with_a_key_the_newest_file_wins(self, app, monkeypatch):
        payload = {
            "bulkDataProductBag": [
                {
                    "fileBag": [
                        {"fileName": "ipg260901.zip", "fileDownloadURI": "https://x/1.zip"},
                        {"fileName": "ipg260908.zip", "fileDownloadURI": "https://x/2.zip"},
                        {"fileName": "readme.txt", "fileDownloadURI": "https://x/r.txt"},
                    ]
                }
            ]
        }
        monkeypatch.setattr(uspto.requests, "get", lambda *a, **k: _Resp(payload=payload))
        with app.app_context():
            app.config["USPTO_ODP_API_KEY"] = "k"
            weekly = uspto.discover_latest()
        assert weekly.name == "ipg260908.zip"
        assert weekly.url == "https://x/2.zip"

    def test_a_broken_key_falls_back_instead_of_failing(self, app, monkeypatch):
        """A key that is present but not working must not take the feature
        down when a derivable URL exists."""

        def boom(*a, **k):
            raise uspto.requests.ConnectionError("down")

        monkeypatch.setattr(uspto.requests, "get", boom)
        with app.app_context():
            app.config["USPTO_ODP_API_KEY"] = "k"
            weekly = uspto.discover_latest(today=date(2026, 9, 10))
        assert weekly.name == "ipg260908.zip"

    def test_extract_files_tolerates_a_renamed_envelope(self):
        found = uspto._extract_files({"any": {"nesting": [{"name": "ipg1.zip", "url": "u"}]}})
        assert found == [{"name": "ipg1.zip", "url": "u"}]


class TestDownload:
    def test_streams_to_disk_and_hashes(self, app, monkeypatch, tmp_path):
        monkeypatch.setattr(
            uspto.requests, "get", lambda *a, **k: _Resp(chunks=[b"hello ", b"world"])
        )
        weekly = uspto.WeeklyFile("ipg260908.zip", "https://x/f.zip", date(2026, 9, 8))
        with app.app_context():
            path, sha = uspto.download(weekly, dest_dir=str(tmp_path))
        assert open(path, "rb").read() == b"hello world"
        assert len(sha) == 64

    def test_an_existing_file_is_not_refetched(self, app, monkeypatch, tmp_path):
        (tmp_path / "ipg260908.zip").write_bytes(b"already here")

        def explode(*a, **k):
            raise AssertionError("should not re-download")

        monkeypatch.setattr(uspto.requests, "get", explode)
        weekly = uspto.WeeklyFile("ipg260908.zip", "https://x/f.zip", date(2026, 9, 8))
        with app.app_context():
            path, _ = uspto.download(weekly, dest_dir=str(tmp_path))
        assert open(path, "rb").read() == b"already here"

    def test_the_size_cap_aborts_and_leaves_no_partial(self, app, monkeypatch, tmp_path):
        monkeypatch.setattr(
            uspto.requests, "get", lambda *a, **k: _Resp(chunks=[b"x" * 2048] * 2000)
        )
        weekly = uspto.WeeklyFile("ipg260908.zip", "https://x/f.zip", date(2026, 9, 8))
        with app.app_context():
            app.config["PATENT_MAX_DOWNLOAD_MB"] = 1
            with pytest.raises(ValueError, match="cap"):
                uspto.download(weekly, dest_dir=str(tmp_path))
        assert list(tmp_path.iterdir()) == []

    def test_zip_is_unpacked_to_the_xml_inside(self, tmp_path):
        archive = tmp_path / "ipg260908.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("ipg260908.xml", _AI)
        extracted = uspto.ensure_xml(str(archive))
        assert extracted.endswith(".xml")
        assert "neural network" in open(extracted, encoding="utf-8").read()


class TestIngest:
    def test_only_matching_cpc_is_written(self, app, weekly_xml):
        """Three patents in, one kept: the filter runs before the write."""
        with app.app_context():
            app.config["PATENT_AI_CPC_CODES"] = "G06N"
            app.config["PATENT_AI_CPC_EXTENDED"] = ""
            run = ingest.ingest_file(weekly_xml)

            assert run.status == "ok"
            assert run.documents_seen == 3
            assert run.documents_kept == 1
            assert [d.doc_number for d in PatentDocument.query.all()] == ["US11123456B2"]

    def test_extended_cpc_widens_the_corpus(self, app, weekly_xml):
        with app.app_context():
            app.config["PATENT_AI_CPC_CODES"] = "G06N"
            app.config["PATENT_AI_CPC_EXTENDED"] = "G06V"
            run = ingest.ingest_file(weekly_xml)
            assert run.documents_kept == 2
            sources = {d.doc_number: d.ai_source for d in PatentDocument.query.all()}
            assert sources["US11123456B2"] == "cpc_core"
            assert sources["US11222333B2"] == "cpc_extended"

    def test_claims_are_stored_with_their_dependency(self, app, weekly_xml):
        with app.app_context():
            ingest.ingest_file(weekly_xml)
            doc = PatentDocument.query.filter_by(doc_number="US11123456B2").one()
            claims = {c.number: c for c in doc.claims}
            assert claims[1].is_independent
            assert claims[2].depends_on == 1

    def test_an_unchanged_document_is_not_rewritten(self, app, weekly_xml):
        with app.app_context():
            first = ingest.ingest_file(weekly_xml)
            second = ingest.ingest_file(weekly_xml)
            assert first.documents_kept == 1
            assert second.documents_kept == 0
            assert PatentDocument.query.count() == 1

    def test_a_changed_document_replaces_its_claims(self, app, weekly_xml, tmp_path):
        with app.app_context():
            ingest.ingest_file(weekly_xml)
            doc_id = PatentDocument.query.one().id
            assert PatentClaim.query.filter_by(patent_document_id=doc_id).count() == 2

            # Same patent, one claim removed -- a corrected grant.
            revised = tmp_path / "ipg260915.xml"
            revised.write_text(
                _AI.split('<claim id="CLM-00002"')[0] + "</claims></us-patent-grant>\n",
                encoding="utf-8",
            )
            ingest.ingest_file(str(revised))

            assert PatentDocument.query.count() == 1
            assert PatentClaim.query.filter_by(patent_document_id=doc_id).count() == 1

    def test_a_failed_run_is_recorded_not_left_running(self, app):
        with app.app_context():
            with pytest.raises(OSError):
                ingest.ingest_file("does-not-exist.xml")
            run = PatentIngestRun.query.order_by(PatentIngestRun.id.desc()).first()
            assert run.status == "error"
            assert run.finished_at is not None


class TestPurge:
    def _doc(self, number, grant_date):
        doc = PatentDocument(
            doc_number=number,
            country="US",
            title="t",
            grant_date=grant_date,
            ai_source="cpc_core",
        )
        db.session.add(doc)
        db.session.flush()
        return doc

    def test_only_what_fell_out_of_the_window_is_removed(self, app):
        with app.app_context():
            app.config["PATENT_WINDOW_WEEKS"] = 3
            today = date(2026, 9, 12)
            self._doc("US1AAA1B2", date(2026, 9, 8))  # inside
            self._doc("US2BBB2B2", date(2026, 7, 1))  # outside
            db.session.commit()

            assert ingest.purge_window(today) == 1
            assert [d.doc_number for d in PatentDocument.query.all()] == ["US1AAA1B2"]

    def test_a_kept_patent_survives_its_corpus_copy(self, app):
        """`paper_id` is SET NULL, not CASCADE: purging the window must never
        delete something a user put in their library."""
        with app.app_context():
            paper = Paper(
                source="patentsview",
                external_id="US9999999",
                title="A patent someone kept",
                kind="patent",
                published_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
            db.session.add(paper)
            db.session.flush()

            doc = self._doc("US3CCC3B2", date(2026, 7, 1))
            doc.paper_id = paper.id
            db.session.commit()
            paper_id = paper.id

            assert ingest.purge_window(date(2026, 9, 12)) == 1
            assert PatentDocument.query.count() == 0
            assert db.session.get(Paper, paper_id) is not None

            db.session.delete(db.session.get(Paper, paper_id))
            db.session.commit()
