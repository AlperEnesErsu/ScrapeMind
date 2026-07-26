"""Faz 3 — interest -> topic classification, the interest-aware source
picker (suggested_sources / effective_source_prefs / user_enabled_sources),
custom user RSS feeds (UserFeed CRUD + ingestion), and the sources-card
grouping render.

No real network: `fetch_feed_conditional` / `rss_source.fetch_feed` are
monkeypatched wherever a feed would otherwise be fetched, and `_call_llm` is
stubbed the same way test_digest.py / test_feeds.py do it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.service import add_user_keyword
from app.modules.scrape import ai_service
from app.modules.scrape.models import Paper, UserFeed, UserPaper
from app.modules.scrape.net_guard import BLOCKED_MESSAGE
from app.modules.scrape.service import (
    FEED_CAP_MESSAGE,
    add_user_feed,
    effective_source_prefs,
    ingest_user_feeds,
    link_relevant_feed_items,
    list_user_feeds,
    remove_user_feed,
    set_user_source,
    toggle_user_feed,
    upsert_paper,
    user_enabled_sources,
)
from app.modules.scrape.sources import suggested_sources
from app.modules.scrape.sources.payload import PaperPayload
from app.modules.scrape.sources.rss_source import FeedFetchResult


@pytest.fixture
def clean_user(db):
    for tbl in (
        "notifications",
        "user_digests",
        "paper_notes",
        "user_papers",
        "papers",
        "user_sources",
        "user_feeds",
        "scan_runs",
        "user_settings",
        "user_keywords",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="topicsuser").delete()
    db.session.commit()
    u = User(
        username="topicsuser",
        email="topicsuser@example.test",
        full_name="Topics User",
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
        "user_feeds",
        "scan_runs",
        "user_settings",
        "user_keywords",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(id=u.id).delete()
    db.session.commit()


def _news_payload(ext_id: str, *, source: str = "user_feed", title: str = "News Title") -> PaperPayload:
    return PaperPayload(
        source=source,
        external_id=ext_id,
        title=title,
        abstract="An announcement.",
        authors=[],
        url=f"https://example.test/{ext_id}",
        pdf_url=None,
        published_at=datetime(2026, 7, 20, tzinfo=UTC),
        categories=["announcement"],
        kind="news",
    )


# ----------------------------------------------------------------------------
# classify_user_topics — lexicon fast-path, LLM fallback, guards, caching
# ----------------------------------------------------------------------------


def test_classify_user_topics_no_keywords_returns_general(app, db, clean_user):
    with app.app_context():
        assert ai_service.classify_user_topics(clean_user) == ["general"]


def test_classify_user_topics_lexicon_resolves_without_llm_call(app, db, clean_user, monkeypatch):
    def _boom(**kw):
        raise AssertionError("LLM must not be called when the lexicon fully resolves")

    monkeypatch.setattr(ai_service, "_call_llm", _boom)
    with app.app_context():
        add_user_keyword(clean_user, "yapay zeka")
        result = ai_service.classify_user_topics(clean_user)
        assert result == ["ai"]


def test_classify_user_topics_lexicon_multiple_keywords(app, db, clean_user, monkeypatch):
    def _boom(**kw):
        raise AssertionError("LLM must not be called when the lexicon fully resolves")

    monkeypatch.setattr(ai_service, "_call_llm", _boom)
    with app.app_context():
        add_user_keyword(clean_user, "tarih")
        add_user_keyword(clean_user, "genetik")
        result = ai_service.classify_user_topics(clean_user)
        assert set(result) == {"humanities", "biomed"}


def test_classify_user_topics_ai_term_does_not_false_match_substring(app, db, clean_user, monkeypatch):
    """'ai' must be matched as a whole word, not as a substring of e.g.
    'explainability' — otherwise every keyword containing those two letters
    would falsely classify as AI."""

    def _fake(**kw):
        return {"topics": ["cs"]}, "{}"

    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        add_user_keyword(clean_user, "explainability")
        result = ai_service.classify_user_topics(clean_user)
        assert result == ["cs"]


def test_classify_user_topics_llm_fallback_for_unresolved(app, db, clean_user, monkeypatch):
    calls = {"n": 0}

    def _fake(**kw):
        calls["n"] += 1
        return {"topics": ["cs", "not_a_real_topic"]}, "{}"

    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        add_user_keyword(clean_user, "obscure gibberish widget term")
        result = ai_service.classify_user_topics(clean_user)
        assert calls["n"] == 1
        # Unknown topic keys returned by the LLM are dropped.
        assert result == ["cs"]


def test_classify_user_topics_combines_lexicon_and_llm(app, db, clean_user, monkeypatch):
    def _fake(**kw):
        return {"topics": ["physics"]}, "{}"

    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        add_user_keyword(clean_user, "tarih")  # lexicon -> humanities
        add_user_keyword(clean_user, "obscure gibberish widget term")  # unresolved -> LLM
        result = ai_service.classify_user_topics(clean_user)
        assert set(result) == {"humanities", "physics"}


def test_classify_user_topics_non_dict_guard_falls_back(app, db, clean_user, monkeypatch):
    calls = {"n": 0}

    def _bad(**kw):
        calls["n"] += 1
        return ["not", "a", "dict"], "[]"

    monkeypatch.setattr(ai_service, "_call_llm", _bad)
    with app.app_context():
        add_user_keyword(clean_user, "obscure gibberish widget term")
        result = ai_service.classify_user_topics(clean_user)
        assert result == ["general"]
        assert calls["n"] == 2  # one repair-retry before giving up


def test_classify_user_topics_caches_across_calls(app, db, clean_user, monkeypatch):
    calls = {"n": 0}

    def _fake(**kw):
        calls["n"] += 1
        return {"topics": ["cs"]}, "{}"

    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        add_user_keyword(clean_user, "obscure gibberish widget term")
        first = ai_service.classify_user_topics(clean_user)
        assert first == ["cs"]
        assert calls["n"] == 1

        second = ai_service.classify_user_topics(clean_user)
        assert second == ["cs"]
        assert calls["n"] == 1  # cache hit — no second LLM call


def test_classify_user_topics_recomputes_when_keywords_change(app, db, clean_user, monkeypatch):
    calls = {"n": 0}

    def _fake(**kw):
        calls["n"] += 1
        return {"topics": ["cs"]}, "{}"

    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        add_user_keyword(clean_user, "obscure gibberish widget term")
        ai_service.classify_user_topics(clean_user)
        assert calls["n"] == 1

        add_user_keyword(clean_user, "another obscure zorbnax term")
        ai_service.classify_user_topics(clean_user)
        assert calls["n"] == 2  # keyword set hash changed -> recompute


# ----------------------------------------------------------------------------
# suggested_sources — topic intersection
# ----------------------------------------------------------------------------

_FEED_KEYS = {"openai_blog", "google_ai_blog", "deepmind_blog", "huggingface_blog"}


def test_suggested_sources_ai_ml_includes_all_feeds():
    result = suggested_sources(["ai", "ml"])
    assert _FEED_KEYS <= result


def test_suggested_sources_humanities_excludes_feeds():
    result = suggested_sources(["humanities"])
    assert result & _FEED_KEYS == set()


def test_suggested_sources_general_excludes_feeds_but_includes_broad_academic():
    result = suggested_sources(["general"])
    assert result & _FEED_KEYS == set()
    assert "arxiv" in result
    assert "semantic_scholar" in result


def test_suggested_sources_empty_topics_returns_empty():
    assert suggested_sources([]) == set()


# ----------------------------------------------------------------------------
# effective_source_prefs / user_enabled_sources — topic-aware defaults
# ----------------------------------------------------------------------------


def test_effective_source_prefs_feed_off_without_topic_overlap(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "classify_user_topics", lambda user: ["humanities"])
    with app.app_context():
        prefs = effective_source_prefs(clean_user)
        assert prefs["openai_blog"] is False
        assert prefs["arxiv"] is True
        assert prefs["semantic_scholar"] is True
        assert prefs["pubmed"] is True


def test_effective_source_prefs_feed_on_with_topic_overlap(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "classify_user_topics", lambda user: ["ai"])
    with app.app_context():
        prefs = effective_source_prefs(clean_user)
        assert prefs["openai_blog"] is True
        assert prefs["huggingface_blog"] is True


def test_effective_source_prefs_general_does_not_force_feeds_on(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "classify_user_topics", lambda user: ["general"])
    with app.app_context():
        prefs = effective_source_prefs(clean_user)
        for key in _FEED_KEYS:
            assert prefs[key] is False


def test_effective_source_prefs_explicit_override_wins(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "classify_user_topics", lambda user: ["humanities"])
    with app.app_context():
        set_user_source(clean_user, "openai_blog", True)
        set_user_source(clean_user, "arxiv", False)
        prefs = effective_source_prefs(clean_user)
        assert prefs["openai_blog"] is True  # explicit ON beats "no topic overlap"
        assert prefs["arxiv"] is False  # explicit OFF beats "academic default on"


def test_user_enabled_sources_excludes_feed_without_topic_overlap(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "classify_user_topics", lambda user: ["humanities"])
    with app.app_context():
        names = set(user_enabled_sources(clean_user).keys())
        assert "openai_blog" not in names
        assert "arxiv" in names
        assert "pubmed" in names


def test_user_enabled_sources_includes_feed_with_topic_overlap(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "classify_user_topics", lambda user: ["ai", "ml"])
    with app.app_context():
        names = set(user_enabled_sources(clean_user).keys())
        assert _FEED_KEYS <= names


# ----------------------------------------------------------------------------
# UserFeed CRUD — add_user_feed / list / remove / toggle
# ----------------------------------------------------------------------------


def _payload(n: int = 1):
    return PaperPayload(
        source="user_feed",
        external_id=f"https://blog.example.test/{n}",
        title=f"Post {n}",
        abstract=None,
        authors=[],
        url=f"https://blog.example.test/{n}",
        pdf_url=None,
        published_at=None,
        categories=["announcement"],
        kind="news",
    )


def _serve_feed(monkeypatch, result: FeedFetchResult):
    """`add_user_feed` validates by fetching once — stub that fetch.

    Note it patches the name *bound in service*, not `rss_source`, because the
    service imports it at module level.
    """
    monkeypatch.setattr(
        "app.modules.scrape.service.fetch_feed_conditional", lambda feed, **kw: result
    )


_OK_FEED = FeedFetchResult([_payload()], "ok", title="Example Blog")


def test_add_user_feed_succeeds_and_autofills_label(app, db, clean_user, monkeypatch):
    _serve_feed(monkeypatch, _OK_FEED)
    with app.app_context():
        feed, err = add_user_feed(clean_user, "blog.example.test/feed.xml")
        assert err is None
        assert feed is not None
        assert feed.url == "https://blog.example.test/feed.xml"  # scheme auto-added
        assert feed.label == "Example Blog"
        assert feed.active is True


def test_add_user_feed_uses_explicit_label(app, db, clean_user, monkeypatch):
    _serve_feed(monkeypatch, _OK_FEED)
    with app.app_context():
        feed, err = add_user_feed(clean_user, "https://blog.example.test/feed.xml", label="My Label")
        assert err is None
        assert feed.label == "My Label"


def test_add_user_feed_rejects_invalid_url(app, db, clean_user, monkeypatch):
    """Blank/whitespace-only input fails normalization before any fetch is
    attempted — guard the fetch with an assertion so a normalization
    regression can never turn this into a real network call."""

    def _boom(*a, **k):
        raise AssertionError("must not fetch for an unparseable URL")

    monkeypatch.setattr("app.modules.scrape.service.fetch_feed_conditional", _boom)
    with app.app_context():
        feed, err = add_user_feed(clean_user, "   ")
        assert feed is None
        assert err

        feed2, err2 = add_user_feed(clean_user, "")
        assert feed2 is None
        assert err2


def test_add_user_feed_rejects_empty_feed(app, db, clean_user, monkeypatch):
    _serve_feed(monkeypatch, FeedFetchResult([], "ok"))
    with app.app_context():
        feed, err = add_user_feed(clean_user, "https://blog.example.test/empty.xml")
        assert feed is None
        assert err


def test_add_user_feed_rejects_on_fetch_failure(app, db, clean_user, monkeypatch):
    for status in ("timeout", "http_error", "parse_error", "too_large"):
        _serve_feed(monkeypatch, FeedFetchResult([], status))
        with app.app_context():
            feed, err = add_user_feed(clean_user, f"https://blog.example.test/{status}.xml")
            assert feed is None, status
            assert err, status


def test_add_user_feed_enforces_cap(app, db, clean_user, monkeypatch):
    """Each active feed is one HTTP request per nightly run, so the list has
    to be bounded — this is the guard against the "hundreds of feeds" case."""
    _serve_feed(monkeypatch, _OK_FEED)
    monkeypatch.setitem(app.config, "MAX_USER_FEEDS", 3)
    with app.app_context():
        for i in range(3):
            feed, err = add_user_feed(clean_user, f"https://blog.example.test/{i}.xml")
            assert err is None, err

        feed, err = add_user_feed(clean_user, "https://blog.example.test/one-too-many.xml")
        assert feed is None
        assert err == FEED_CAP_MESSAGE
        assert UserFeed.query.filter_by(user_id=clean_user.id).count() == 3

        # Re-adding an existing URL consumes no slot, so it still works at cap
        again, err2 = add_user_feed(clean_user, "https://blog.example.test/0.xml")
        assert err2 is None
        assert again is not None


def test_add_user_feed_rejects_private_host(app, db, clean_user, monkeypatch):
    """A feed URL is fetched server-side, so localhost / link-local / cloud
    metadata addresses must be refused before anything is fetched."""

    def _boom(*a, **k):
        raise AssertionError("must not fetch a blocked address")

    monkeypatch.setattr("app.modules.scrape.service.fetch_feed_conditional", _boom)
    monkeypatch.setitem(app.config, "FEED_ALLOW_PRIVATE_HOSTS", False)
    with app.app_context():
        for url in (
            "http://127.0.0.1:80/feed.xml",
            "http://10.0.0.5/feed.xml",
            "http://169.254.169.254/latest/meta-data/",
        ):
            feed, err = add_user_feed(clean_user, url)
            assert feed is None, url
            assert err == BLOCKED_MESSAGE
        assert UserFeed.query.filter_by(user_id=clean_user.id).count() == 0


def test_add_user_feed_is_idempotent_per_url(app, db, clean_user, monkeypatch):
    _serve_feed(monkeypatch, _OK_FEED)
    with app.app_context():
        feed1, _ = add_user_feed(clean_user, "https://blog.example.test/feed.xml")
        feed2, _ = add_user_feed(clean_user, "https://blog.example.test/feed.xml")
        assert feed1.id == feed2.id
        assert UserFeed.query.filter_by(user_id=clean_user.id).count() == 1


def test_list_remove_toggle_user_feed(app, db, clean_user, monkeypatch):
    _serve_feed(monkeypatch, _OK_FEED)
    with app.app_context():
        feed, _ = add_user_feed(clean_user, "https://blog.example.test/feed.xml")
        assert len(list_user_feeds(clean_user)) == 1

        new_val = toggle_user_feed(clean_user, feed.id)
        assert new_val is False
        assert list_user_feeds(clean_user)[0].active is False

        assert toggle_user_feed(clean_user, 10_000_000) is None  # not found

        assert remove_user_feed(clean_user, feed.id) is True
        assert list_user_feeds(clean_user) == []
        assert remove_user_feed(clean_user, feed.id) is False  # already gone


# ----------------------------------------------------------------------------
# Custom feed ingestion (source="user_feed" dedup) + relevance-linking inclusion
# ----------------------------------------------------------------------------


def test_ingest_user_feeds_upserts_papers_shared_dedup(app, db, clean_user, monkeypatch):
    from app.modules.scrape.sources import rss_source

    payloads = [_news_payload("cf-1"), _news_payload("cf-2")]
    monkeypatch.setattr(rss_source, "fetch_feed", lambda feed: payloads)

    row = UserFeed(user_id=clean_user.id, url="https://blog.example.test/feed.xml", active=True)
    db.session.add(row)
    db.session.commit()

    with app.app_context():
        summary, touched = ingest_user_feeds(clean_user)
        assert summary["new"] == 2
        assert len(touched) == 2
        rows = Paper.query.filter_by(source="user_feed").all()
        assert len(rows) == 2

        # Re-ingest: no new rows (dedup), but touched still reflects the fetch.
        summary2, touched2 = ingest_user_feeds(clean_user)
        assert summary2["new"] == 0
        assert len(touched2) == 2
        assert Paper.query.filter_by(source="user_feed").count() == 2


def test_ingest_user_feeds_no_active_feeds_short_circuits(app, db, clean_user):
    with app.app_context():
        summary, touched = ingest_user_feeds(clean_user)
        assert summary == {"hits": 0, "new": 0}
        assert touched == []


def test_ingest_user_feeds_skips_inactive_feeds(app, db, clean_user, monkeypatch):
    from app.modules.scrape.sources import rss_source

    def _boom(feed):
        raise AssertionError("an inactive feed must not be fetched")

    monkeypatch.setattr(rss_source, "fetch_feed", _boom)
    row = UserFeed(user_id=clean_user.id, url="https://blog.example.test/feed.xml", active=False)
    db.session.add(row)
    db.session.commit()

    with app.app_context():
        summary, touched = ingest_user_feeds(clean_user)
        assert summary == {"hits": 0, "new": 0}
        assert touched == []


def test_link_relevant_feed_items_includes_custom_feed_candidates(app, db, clean_user, monkeypatch):
    with app.app_context():
        add_user_keyword(clean_user, "x")
        custom_paper = upsert_paper(_news_payload("cf-relevant", source="user_feed"))

        fake_scores = [
            {"paper_id": custom_paper.id, "score": 90, "why": "alakalı", "matched_keyword": "x"},
        ]
        monkeypatch.setattr(ai_service, "score_feed_relevance", lambda user, papers: fake_scores)

        # Without extra_candidates, the user_feed paper is never a candidate
        # (it's outside the curated-feed source-key query).
        seen = {}

        def _capture(user, papers):
            seen["ids"] = {p.id for p in papers}
            return []

        monkeypatch.setattr(ai_service, "score_feed_relevance", _capture)
        result_no_extra = link_relevant_feed_items(clean_user, threshold=60)
        assert custom_paper.id not in seen.get("ids", set())
        assert result_no_extra["reason"] == "no_candidates"

        # With extra_candidates, it's included and gets scored/linked.
        monkeypatch.setattr(ai_service, "score_feed_relevance", lambda user, papers: fake_scores)
        result = link_relevant_feed_items(
            clean_user, threshold=60, extra_candidates=[custom_paper]
        )
        assert result["linked"] == 1
        linked_ids = {up.paper_id for up in UserPaper.query.filter_by(user_id=clean_user.id).all()}
        assert custom_paper.id in linked_ids


def test_link_relevant_feed_items_extra_candidates_dedupe_already_linked(app, db, clean_user, monkeypatch):
    with app.app_context():
        add_user_keyword(clean_user, "x")
        custom_paper = upsert_paper(_news_payload("cf-already-linked", source="user_feed"))
        from app.modules.scrape.service import link_user_paper

        link_user_paper(clean_user, custom_paper, matched_keyword="x")

        def _boom(user, papers):
            raise AssertionError("an already-linked paper must not be re-scored")

        monkeypatch.setattr(ai_service, "score_feed_relevance", _boom)
        result = link_relevant_feed_items(
            clean_user, threshold=60, extra_candidates=[custom_paper]
        )
        assert result == {"scored": 0, "linked": 0, "reason": "no_candidates"}


def test_feed_link_task_ingests_custom_feed_then_links(app, db, clean_user, monkeypatch):
    from app.modules.scrape.sources import rss_source
    from app.tasks import feed_tasks

    add_user_keyword(clean_user, "x")
    payloads = [_news_payload("task-cf-1", title="Custom Feed Post")]
    monkeypatch.setattr(rss_source, "fetch_feed", lambda feed: payloads)

    row = UserFeed(user_id=clean_user.id, url="https://blog.example.test/feed.xml", active=True)
    db.session.add(row)
    db.session.commit()

    captured = {}

    def _fake_score(user, papers):
        captured["ids"] = {p.id for p in papers}
        return [{"paper_id": p.id, "score": 80, "why": "x", "matched_keyword": "x"} for p in papers]

    monkeypatch.setattr(ai_service, "score_feed_relevance", _fake_score)

    with app.app_context():
        result = feed_tasks.link_for_user.delay(clean_user.id, threshold=60).get()
        assert result["linked"] >= 1
        paper = Paper.query.filter_by(source="user_feed", external_id="task-cf-1").first()
        assert paper is not None
        assert paper.id in captured["ids"]
