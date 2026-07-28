"""Faz 2 — RSS feed global ingestion + per-user LLM relevance scoring/linking.

No real network: `rss_source.fetch_feed` is monkeypatched to return canned
`PaperPayload` lists (live-parsing itself is covered in
test_scrape_sources.py). LLM calls go through the same `_call_llm`
monkeypatch pattern used throughout test_digest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.service import add_user_keyword
from app.modules.scrape import ai_service
from app.modules.scrape.models import Paper, UserPaper
from app.modules.scrape.service import (
    link_relevant_feed_items,
    list_user_source_prefs,
    set_user_source,
    upsert_paper,
)
from app.modules.scrape.sources import rss_source
from app.modules.scrape.sources.payload import PaperPayload
from app.tasks import feed_tasks


def _news_payload(
    ext_id: str, *, source: str = "openai_blog", title: str = "News Title"
) -> PaperPayload:
    return PaperPayload(
        source=source,
        external_id=ext_id,
        title=title,
        abstract="An announcement about something new.",
        authors=[],
        url=f"https://example.test/{ext_id}",
        pdf_url=None,
        published_at=datetime(2026, 7, 20, tzinfo=UTC),
        categories=["announcement"],
        kind="news",
    )


@pytest.fixture
def clean_user(db):
    for tbl in (
        "notifications",
        "user_digests",
        "paper_notes",
        "user_papers",
        "papers",
        "user_sources",
        "user_settings",
        "user_keywords",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="feeduser").delete()
    db.session.commit()
    u = User(
        username="feeduser",
        email="feeduser@example.test",
        full_name="Feed User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    for tbl in (
        "notifications",
        "user_digests",
        "paper_notes",
        "user_papers",
        "papers",
        "user_sources",
        "user_settings",
        "user_keywords",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(id=u.id).delete()
    db.session.commit()


# ----------------------------------------------------------------------------
# feeds.ingest_all — global fetch + Paper upsert, dedup
# ----------------------------------------------------------------------------


def test_ingest_all_upserts_news_papers(app, db, clean_user, monkeypatch):
    payloads = [_news_payload("post-1"), _news_payload("post-2")]
    monkeypatch.setattr(
        rss_source, "fetch_feed", lambda feed: payloads if feed["key"] == "openai_blog" else []
    )
    with app.app_context():
        result = feed_tasks.ingest_all.delay().get()
        assert result["new"] == 2
        assert result["feeds"]["openai_blog"] == 2
        rows = Paper.query.filter_by(source="openai_blog", kind="news").all()
        assert len(rows) == 2
        assert {r.external_id for r in rows} == {"post-1", "post-2"}


def test_ingest_all_dedupes_on_rerun(app, db, clean_user, monkeypatch):
    payloads = [_news_payload("dup-1")]
    monkeypatch.setattr(
        rss_source, "fetch_feed", lambda feed: payloads if feed["key"] == "openai_blog" else []
    )
    with app.app_context():
        feed_tasks.ingest_all.delay().get()
        result2 = feed_tasks.ingest_all.delay().get()
        assert result2["new"] == 0
        assert Paper.query.filter_by(source="openai_blog", external_id="dup-1").count() == 1


def test_ingest_all_survives_one_feed_failing(app, db, clean_user, monkeypatch):
    def _fetch(feed):
        if feed["key"] == "openai_blog":
            raise RuntimeError("network unreachable")
        return [_news_payload(f"{feed['key']}-1", source=feed["key"])]

    monkeypatch.setattr(rss_source, "fetch_feed", _fetch)
    with app.app_context():
        result = feed_tasks.ingest_all.delay().get()
        assert result["feeds"]["openai_blog"] == -1
        # The other three feeds still landed.
        assert Paper.query.filter_by(kind="news").count() == 3


# ----------------------------------------------------------------------------
# ai_service.score_feed_relevance — single batch LLM call + dict-guard
# ----------------------------------------------------------------------------


def test_score_feed_relevance_maps_scores_to_paper_ids(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    with app.app_context():
        add_user_keyword(clean_user, "transformers")
        p1 = upsert_paper(_news_payload("s1", title="Transformer News"))
        p2 = upsert_paper(_news_payload("s2", title="Unrelated"))

        fake = {
            "scores": [
                {"ref": 1, "score": 85, "why": "çok alakalı", "matched_keyword": "transformers"},
                {"ref": 2, "score": 10, "why": "alakasız", "matched_keyword": None},
            ]
        }
        monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (fake, "{}"))

        out = ai_service.score_feed_relevance(clean_user, [p1, p2])
        assert len(out) == 2
        by_id = {o["paper_id"]: o for o in out}
        assert by_id[p1.id]["score"] == 85
        assert by_id[p1.id]["matched_keyword"] == "transformers"
        assert by_id[p2.id]["score"] == 10


def test_score_feed_relevance_non_object_json_guards_no_crash(app, db, clean_user, monkeypatch):
    """Small/free models sometimes emit a bare list/string instead of the
    {"scores": [...]} object — must retry once, then give up returning []
    rather than crashing on `.get`."""
    calls = {"n": 0}

    def _bad(**kw):
        calls["n"] += 1
        return ["not", "a", "dict"], "[]"

    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", _bad)
    with app.app_context():
        add_user_keyword(clean_user, "xx")
        p = upsert_paper(_news_payload("s3"))
        out = ai_service.score_feed_relevance(clean_user, [p])
        assert out == []
        assert calls["n"] == 2  # one repair-retry before giving up


def test_score_feed_relevance_ai_disabled_no_call(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)

    def _boom(**kw):
        raise AssertionError("LLM must not be called when AI is disabled")

    monkeypatch.setattr(ai_service, "_call_llm", _boom)
    with app.app_context():
        add_user_keyword(clean_user, "x")
        p = upsert_paper(_news_payload("s4"))
        assert ai_service.score_feed_relevance(clean_user, [p]) == []


def test_score_feed_relevance_no_keywords_no_call(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)

    def _boom(**kw):
        raise AssertionError("LLM must not be called when the user has no interests")

    monkeypatch.setattr(ai_service, "_call_llm", _boom)
    with app.app_context():
        p = upsert_paper(_news_payload("s5"))
        assert ai_service.score_feed_relevance(clean_user, [p]) == []


def test_score_feed_relevance_empty_papers_no_call(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)

    def _boom(**kw):
        raise AssertionError("LLM must not be called for an empty batch")

    monkeypatch.setattr(ai_service, "_call_llm", _boom)
    with app.app_context():
        add_user_keyword(clean_user, "x")
        assert ai_service.score_feed_relevance(clean_user, []) == []


# ----------------------------------------------------------------------------
# service.link_relevant_feed_items — threshold filter + per-feed mute skip
# ----------------------------------------------------------------------------


def test_link_relevant_feed_items_links_above_threshold_only(app, db, clean_user, monkeypatch):
    with app.app_context():
        add_user_keyword(clean_user, "transformers")
        p1 = upsert_paper(_news_payload("l1", title="Transformer News"))
        p2 = upsert_paper(_news_payload("l2", title="Unrelated"))

        fake_scores = [
            {"paper_id": p1.id, "score": 80, "why": "x", "matched_keyword": "transformers"},
            {"paper_id": p2.id, "score": 20, "why": "y", "matched_keyword": None},
        ]
        monkeypatch.setattr(ai_service, "score_feed_relevance", lambda user, papers: fake_scores)

        result = link_relevant_feed_items(clean_user, threshold=60)
        assert result["linked"] == 1
        linked_ids = {up.paper_id for up in UserPaper.query.filter_by(user_id=clean_user.id).all()}
        assert p1.id in linked_ids
        assert p2.id not in linked_ids


def test_link_relevant_feed_items_skips_when_all_feeds_muted(app, db, clean_user, monkeypatch):
    with app.app_context():
        for f in rss_source.FEEDS:
            set_user_source(clean_user, f["key"], False)

        def _boom(user, papers):
            raise AssertionError("LLM must not be called when every feed is muted")

        monkeypatch.setattr(ai_service, "score_feed_relevance", _boom)
        result = link_relevant_feed_items(clean_user, threshold=60)
        assert result == {"scored": 0, "linked": 0, "reason": "news_muted"}


def test_link_relevant_feed_items_respects_per_feed_mute(app, db, clean_user, monkeypatch):
    """Muting just one feed must not silence the others — opt-out here is
    per-feed, mirroring the academic sources."""
    with app.app_context():
        add_user_keyword(clean_user, "x")
        set_user_source(clean_user, "openai_blog", False)
        assert list_user_source_prefs(clean_user).get("openai_blog") is False

        upsert_paper(_news_payload("m1", source="openai_blog"))
        active_paper = upsert_paper(_news_payload("m2", source="google_ai_blog"))

        seen_paper_ids = {}

        def _capture(user, papers):
            seen_paper_ids["ids"] = {p.id for p in papers}
            return []

        monkeypatch.setattr(ai_service, "score_feed_relevance", _capture)
        link_relevant_feed_items(clean_user, threshold=60)
        assert seen_paper_ids["ids"] == {active_paper.id}


def test_link_relevant_feed_items_no_candidates_short_circuits(app, db, clean_user, monkeypatch):
    with app.app_context():
        add_user_keyword(clean_user, "x")

        def _boom(user, papers):
            raise AssertionError("LLM must not be called with nothing to score")

        monkeypatch.setattr(ai_service, "score_feed_relevance", _boom)
        result = link_relevant_feed_items(clean_user, threshold=60)
        assert result == {"scored": 0, "linked": 0, "reason": "no_candidates"}


# ----------------------------------------------------------------------------
# feeds.link_for_user / link_for_all_users — Celery fan-out
# ----------------------------------------------------------------------------


def test_feed_link_task_missing_user_is_a_noop(app, db):
    with app.app_context():
        result = feed_tasks.link_for_user.delay(10_000_000).get()
        assert result == {"reason": "user_missing"}


def test_feed_link_fanout_queues_active_users(app, db, clean_user, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.tasks.feed_tasks.link_for_user.apply_async",
        lambda **kw: calls.append(kw),
    )
    with app.app_context():
        result = feed_tasks.link_for_all_users()
        assert result["queued"] >= 1
        assert any(
            c["args"] == (clean_user.id,) and c["kwargs"] == {"threshold": 60} for c in calls
        )


def test_fanout_offset_is_deterministic_and_bounded(app):
    """The offset must be a pure function of the user id: it is what makes the
    "next scan at 03:27" we show a user true rather than merely plausible."""
    from app.tasks.fanout import user_fanout_offset

    with app.app_context():
        assert user_fanout_offset(42, 1800) == user_fanout_offset(42, 1800)
        assert user_fanout_offset(42, 1800) != user_fanout_offset(43, 1800)
        assert all(0 <= user_fanout_offset(i, 1800) < 1800 for i in range(1, 500))
        assert user_fanout_offset(42, 0) == 0  # window disabled -> no spread
