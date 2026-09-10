"""The full-text fetch task, and the one line in it that matters.

`fulltext.py` decides whether a licence permits storage; this task is the only
place that decision is written to the database. A test on the gate alone would
not catch a task that fetched correctly and then stored the text anyway, which
is the failure that would actually republish someone's work.
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.scrape.fulltext import FullText, FullTextError
from app.modules.scrape.models import Paper
from app.tasks import fulltext_tasks


@pytest.fixture
def oa_paper(db):
    """A paper with a fetchable OA location and no attempt recorded."""

    def _make(*, license_: str | None, oa_url: str = "https://oa.test/paper.pdf") -> Paper:
        paper = Paper(
            source="openalex",
            # Unique per call. A counter is not enough: rows outlive the test
            # that made them, so a per-test counter collides with the previous
            # test's W0 rather than with its own.
            external_id=f"W{uuid.uuid4().hex[:12]}",
            title="A paper about pomegranates",
            abstract="Short abstract.",
            oa_status="gold",
            oa_license=license_,
            oa_url=oa_url,
        )
        db.session.add(paper)
        db.session.commit()
        return paper

    return _make


def _returns(monkeypatch, text: str, *, storable: bool, license_: str | None):
    monkeypatch.setattr(
        fulltext_tasks,
        "fetch_fulltext",
        lambda url, license_=None: FullText(
            text=text, chars=len(text), storable=storable, license=license_
        ),
    )


def test_redistributable_licence_stores_the_text(db, oa_paper, monkeypatch):
    paper = oa_paper(license_="cc-by")
    _returns(monkeypatch, "full body text", storable=True, license_="cc-by")

    assert fulltext_tasks.fetch_for_paper_task(paper.id) is True

    db.session.refresh(paper)
    assert paper.fulltext == "full body text"
    assert paper.fulltext_chars == 14
    assert paper.fulltext_fetched_at is not None


def test_readable_but_unlicensed_text_is_used_and_not_kept(db, oa_paper, monkeypatch):
    """Bronze OA: free to read, no licence, nothing to republish.

    The character count is still recorded, exactly as `VideoSummary` records
    `transcript_chars` without the transcript -- "we read 41k characters" and
    "we never looked" have to stay distinguishable.
    """
    paper = oa_paper(license_=None)
    _returns(monkeypatch, "full body text", storable=False, license_=None)

    assert fulltext_tasks.fetch_for_paper_task(paper.id) is True

    db.session.refresh(paper)
    assert paper.fulltext is None, "text without a redistribution licence must not be stored"
    assert paper.fulltext_chars == 14
    assert paper.fulltext_fetched_at is not None


def test_permanent_failure_leaves_the_queue(db, oa_paper, monkeypatch):
    """A scanned PDF will not become readable by asking again.

    Recording the attempt is what stops the nightly sweep re-fetching it for
    the life of the deployment.
    """
    paper = oa_paper(license_="cc-by")
    monkeypatch.setattr(
        fulltext_tasks,
        "fetch_fulltext",
        lambda *a, **kw: (_ for _ in ()).throw(FullTextError("extracted only 12 characters")),
    )

    assert fulltext_tasks.fetch_for_paper_task(paper.id) is False

    db.session.refresh(paper)
    assert paper.fulltext_chars == 0
    assert paper.fulltext_fetched_at is not None, "a permanent failure is still an attempt"


def test_transient_failure_stays_in_the_queue(db, oa_paper, monkeypatch):
    """A timeout is not a verdict on the document."""
    paper = oa_paper(license_="cc-by")
    monkeypatch.setattr(
        fulltext_tasks,
        "fetch_fulltext",
        lambda *a, **kw: (_ for _ in ()).throw(FullTextError("request failed: timed out")),
    )

    assert fulltext_tasks.fetch_for_paper_task(paper.id) is False

    db.session.refresh(paper)
    assert paper.fulltext_fetched_at is None, "a transient failure must be retried"
    assert paper.fulltext_chars is None


def test_paper_without_an_oa_location_is_skipped(db, monkeypatch):
    """`oa_url` is only ever set from `best_oa_location`, so its absence means
    there is nothing we are permitted to fetch."""
    paper = Paper(source="arxiv", external_id="2401.1", title="Closed paper")
    db.session.add(paper)
    db.session.commit()

    called = []
    monkeypatch.setattr(fulltext_tasks, "fetch_fulltext", lambda *a, **kw: called.append(a))

    assert fulltext_tasks.fetch_for_paper_task(paper.id) is False
    assert called == []


def test_sweep_skips_papers_already_attempted(db, oa_paper, monkeypatch):
    """`fulltext_fetched_at` is what takes a paper out of the queue.

    Asserted against the two papers this test creates rather than against the
    whole table: other tests leave rows behind on purpose -- a transient
    failure is *supposed* to stay pending -- and an assertion that the sweep
    saw exactly one URL would be testing the fixture, not the filter.
    """
    from datetime import UTC, datetime

    pending = oa_paper(license_="cc-by", oa_url="https://oa.test/pending.pdf")
    done = oa_paper(license_="cc-by", oa_url="https://oa.test/done.pdf")
    done.fulltext_fetched_at = datetime.now(UTC)
    db.session.commit()

    seen = []

    def _fetch(url, license_=None):
        seen.append(url)
        return FullText(text="x" * 600, chars=600, storable=True, license=license_)

    monkeypatch.setattr(fulltext_tasks, "fetch_fulltext", _fetch)
    fulltext_tasks.fetch_pending_task(limit=50)

    assert pending.oa_url in seen
    assert done.oa_url not in seen, "an attempted paper must not be fetched again"


def test_sweep_survives_one_hostile_document(db, oa_paper, monkeypatch):
    """One paper that blows up must not cost the rest of the batch."""
    oa_paper(license_="cc-by", oa_url="https://oa.test/a.pdf")
    oa_paper(license_="cc-by", oa_url="https://oa.test/b.pdf")
    db.session.commit()

    def _fetch(url, license_=None):
        if url.endswith("a.pdf"):
            raise MemoryError("pathological PDF")
        return FullText(text="y" * 600, chars=600, storable=True, license=license_)

    monkeypatch.setattr(fulltext_tasks, "fetch_fulltext", _fetch)

    assert fulltext_tasks.fetch_pending_task(limit=10) == 1
