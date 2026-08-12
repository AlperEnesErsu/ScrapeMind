"""Prior-art search page + live patent search (Faz 5.2).

The load-bearing property here is that this path **persists nothing** — a
one-off idea check must not fill `papers` with rows no user's feed wanted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.modules.scrape import service
from app.modules.scrape.models import Paper
from app.modules.scrape.routes import _prior_art_terms
from app.modules.scrape.sources.payload import PaperPayload


def _payload(external_id="EP123A1", title="A vibration sensor", source="epo_ops"):
    return PaperPayload(
        source=source,
        external_id=external_id,
        title=title,
        abstract="Detects bearing wear.",
        authors=["Jane Smith"],
        url="https://example.test/patent",
        pdf_url=None,
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        categories=["G06N3/08", "assignee:Example Robotics Inc."],
        kind="patent",
        doi=None,
    )


class _FakeSource:
    def __init__(self, payloads=None, error=None):
        self._payloads = payloads or []
        self._error = error

    def search_for_keywords(self, keywords, *, max_results=25):
        if self._error:
            raise self._error
        return self._payloads


@pytest.fixture
def a_user(db):
    """Cleans up after itself, audit rows first.

    The search route calls `log_action`, and `tests/modules/test_scrape.py`'s
    `clean` fixture deletes every user without touching `audit_logs` — a
    leaked audit row here becomes a foreign-key violation in an unrelated
    file. Same reason `tests/core/test_system_settings.py` unwires its admin.
    """
    from sqlalchemy import text

    user = User.query.filter_by(email="priorart@example.test").first()
    if user is None:
        user = User(
            username="priorartuser",
            email="priorart@example.test",
            full_name="Prior Art User",
            password_hash=generate_password_hash("password123"),
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
    uid = user.id

    yield user

    db.session.rollback()
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


@pytest.fixture
def logged_in(client, a_user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(a_user.id)
        sess["_fresh"] = True
    return client


# ----------------------------------------------------------------------------
# Term extraction
# ----------------------------------------------------------------------------


def test_terms_drop_stopwords_and_prefer_long_words():
    terms = _prior_art_terms("A system for detecting bearing wear with vibration sensors")
    assert "for" not in terms and "with" not in terms
    assert "detecting" in terms and "vibration" in terms


def test_terms_are_deduplicated_and_capped():
    idea = " ".join(["telemetry"] * 5 + ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"])
    terms = _prior_art_terms(idea)
    assert terms.count("telemetry") == 1
    assert len(terms) <= 6


def test_turkish_stopwords_are_dropped():
    terms = _prior_art_terms("Rulman aşınmasını tespit eden bir titreşim sensörü")
    assert "bir" not in terms
    assert "titreşim" in terms


def test_empty_idea_yields_no_terms():
    assert _prior_art_terms("") == []


# ----------------------------------------------------------------------------
# search_patents_live — the no-persist guarantee
# ----------------------------------------------------------------------------


def test_live_search_does_not_write_papers(app, db, monkeypatch):
    """The whole reason this is a separate function from
    `scrape_patents_for_user`."""
    monkeypatch.setattr(
        service, "patent_sources", lambda user=None: {"epo_ops": _FakeSource([_payload()])}
    )

    before = Paper.query.count()
    results, per_source = service.search_patents_live(["vibration"])

    assert len(results) == 1
    assert Paper.query.count() == before
    assert Paper.query.filter_by(external_id="EP123A1").first() is None
    assert per_source == {"epo_ops": 1}


def test_live_search_reports_a_failing_source_with_the_sentinel(app, db, monkeypatch):
    """-1 is the same sentinel the scan paths use, so the page can say "EPO
    was unreachable" instead of quietly showing a shorter list."""
    monkeypatch.setattr(
        service,
        "patent_sources",
        lambda user=None: {
            "epo_ops": _FakeSource(error=RuntimeError("boom")),
            "patentsview": _FakeSource([_payload(source="patentsview")]),
        },
    )

    results, per_source = service.search_patents_live(["vibration"])
    assert per_source["epo_ops"] == -1
    assert len(results) == 1  # the healthy source still lands


def test_live_search_sorts_newest_first(app, db, monkeypatch):
    old = _payload(external_id="EP1")
    new = _payload(external_id="EP2")
    object.__setattr__(new, "published_at", datetime(2025, 6, 1, tzinfo=UTC))
    monkeypatch.setattr(
        service, "patent_sources", lambda user=None: {"epo_ops": _FakeSource([old, new])}
    )

    results, _ = service.search_patents_live(["x"])
    assert [r.external_id for r in results] == ["EP2", "EP1"]


def test_live_search_with_no_terms_calls_nothing(app, db, monkeypatch):
    def boom(user=None):
        raise AssertionError("should not have resolved sources")

    monkeypatch.setattr(service, "patent_sources", boom)
    assert service.search_patents_live(["  "]) == ([], {})


# ----------------------------------------------------------------------------
# The page
# ----------------------------------------------------------------------------


def test_page_requires_login(client):
    resp = client.get("/papers/patents")
    assert resp.status_code in (301, 302)
    assert "/auth/login" in resp.headers["Location"]


def test_page_explains_itself_when_no_source_is_available(logged_in, monkeypatch):
    """Without keys or the admin opt-in there is nothing to search. Saying so
    beats an empty box that silently finds nothing."""
    monkeypatch.setattr("app.modules.scrape.service.patent_sources", lambda user=None: {})

    body = logged_in.get("/papers/patents").get_data(as_text=True)
    assert "No patent source is available" in body or "kullanılabilir patent kaynağı yok" in body


def test_search_renders_results_without_persisting(logged_in, db, monkeypatch):
    monkeypatch.setattr(
        "app.modules.scrape.service.patent_sources",
        lambda user=None: {"epo_ops": _FakeSource([_payload()])},
    )
    before = Paper.query.count()

    resp = logged_in.post(
        "/papers/patents",
        data={"idea": "A vibration sensor detecting bearing wear in CNC spindles", "assess": ""},
    )
    assert resp.status_code == 200
    assert "EP123A1" in resp.get_data(as_text=True)
    assert Paper.query.count() == before


def test_assessment_is_skipped_when_not_requested(logged_in, monkeypatch):
    called = []

    monkeypatch.setattr(
        "app.modules.scrape.service.patent_sources",
        lambda user=None: {"epo_ops": _FakeSource([_payload()])},
    )
    monkeypatch.setattr(
        "app.modules.scrape.ai_service.analyze_novelty",
        lambda *a, **k: called.append(1),
    )

    logged_in.post("/papers/patents", data={"idea": "A vibration sensor for spindles"})
    assert called == []


def test_failing_assessment_does_not_cost_the_user_their_results(logged_in, monkeypatch):
    """The patent list is the half with evidentiary value; the LLM output is a
    reading aid over it."""
    monkeypatch.setattr(
        "app.modules.scrape.service.patent_sources",
        lambda user=None: {"epo_ops": _FakeSource([_payload()])},
    )
    monkeypatch.setattr("app.modules.scrape.ai_service.is_ai_enabled", lambda user=None: True)

    def boom(*args, **kwargs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr("app.modules.scrape.ai_service.analyze_novelty", boom)

    resp = logged_in.post(
        "/papers/patents",
        data={"idea": "A vibration sensor detecting bearing wear", "assess": "y"},
    )
    assert resp.status_code == 200
    assert "EP123A1" in resp.get_data(as_text=True)


def test_short_idea_is_rejected(logged_in, monkeypatch):
    monkeypatch.setattr(
        "app.modules.scrape.service.patent_sources",
        lambda user=None: {"epo_ops": _FakeSource([_payload()])},
    )
    resp = logged_in.post("/papers/patents", data={"idea": "hi"})
    assert resp.status_code == 200
    # Rendered the form again rather than searching.
    assert "EP123A1" not in resp.get_data(as_text=True)
