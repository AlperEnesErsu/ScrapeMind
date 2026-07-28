"""Keyword → English query expansion (Faz 3 Bölüm E).

Users type research interests in Turkish; every source we scrape is an
English corpus. These tests cover the three layers that fix that:

  * `ai_service.translate_keywords` — lexicon fast path, one batched LLM call
    for the rest, and the guards around a model that answers badly.
  * `service.ensure_keyword_translations` — persistence on the global
    `Keyword` row, which is what makes the second user to follow a term free.
  * `service.keyword_search_terms` / `_match_keyword` — how the expansion is
    handed to each source, and how a hit found under an English term is filed
    back under the keyword the user actually typed.

`_call_llm` is always stubbed; nothing here touches a real provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.models import Keyword, UserKeyword
from app.modules.scrape import ai_service
from app.modules.scrape.service import (
    _match_keyword,
    ensure_keyword_translations,
    keyword_search_terms,
    scrape_for_user,
)
from app.modules.scrape.sources.payload import PaperPayload


@pytest.fixture
def clean_user(db):
    for tbl in (
        "notifications",
        "user_digests",
        "paper_notes",
        "user_papers",
        "papers",
        "user_sources",
        "scan_runs",
        "user_settings",
        "user_keywords",
        "keywords",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="kwuser").delete()
    db.session.commit()
    u = User(
        username="kwuser",
        email="kwuser@example.test",
        full_name="Keyword User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(text("DELETE FROM user_papers"))
    db.session.execute(text("DELETE FROM papers"))
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
    db.session.query(User).filter_by(username="kwuser").delete()
    db.session.commit()


def _follow(db, user, *values) -> list[Keyword]:
    """Create global keywords and have `user` follow them."""
    rows = []
    for value in values:
        kw = Keyword(value=value)
        db.session.add(kw)
        db.session.commit()
        db.session.add(UserKeyword(user_id=user.id, keyword_id=kw.id))
        db.session.commit()
        rows.append(kw)
    return rows


def _payload(ext_id: str, *, title: str) -> PaperPayload:
    return PaperPayload(
        source="arxiv",
        external_id=ext_id,
        title=title,
        abstract="abs",
        authors=["A. One"],
        url=f"http://arxiv.org/abs/{ext_id}",
        pdf_url=f"http://arxiv.org/pdf/{ext_id}",
        published_at=datetime(2026, 1, 15, tzinfo=UTC),
        categories=["cs.LG"],
    )


def _explode(**_kwargs):
    raise AssertionError("the LLM must not be called here")


# ----------------------------------------------------------------------------
# translate_keywords
# ----------------------------------------------------------------------------


def test_lexicon_terms_never_reach_the_llm(app, monkeypatch):
    monkeypatch.setattr(ai_service, "_call_llm", _explode)
    with app.app_context():
        out = ai_service.translate_keywords(["yapay zeka", "makine öğrenmesi"])
    assert out["yapay zeka"]["en"] == "artificial intelligence"
    assert out["makine öğrenmesi"]["en"] == "machine learning"


def test_english_lexicon_terms_map_to_themselves(app, monkeypatch):
    monkeypatch.setattr(ai_service, "_call_llm", _explode)
    with app.app_context():
        out = ai_service.translate_keywords(["machine learning"])
    assert out["machine learning"]["en"] == "machine learning"


def test_unresolved_terms_go_to_one_batched_call(app, monkeypatch):
    calls = []

    def _fake(*, system, user_msg, max_tokens, user=None):  # noqa: ARG001
        calls.append(user_msg)
        return {
            "translations": [
                {"term": "miyokard perfüzyonu", "en": "myocardial perfusion", "variants": []},
                {"term": "safra kesesi", "en": "gallbladder", "variants": ["cholecyst"]},
            ]
        }, "raw"

    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        out = ai_service.translate_keywords(["yapay zeka", "miyokard perfüzyonu", "safra kesesi"])

    # One call for both unresolved terms; the lexicon term is not in the prompt.
    assert len(calls) == 1
    assert "yapay zeka" not in calls[0]
    assert out["miyokard perfüzyonu"]["en"] == "myocardial perfusion"
    assert out["safra kesesi"]["variants"] == ["cholecyst"]
    assert out["yapay zeka"]["en"] == "artificial intelligence"


def test_echoed_term_matches_case_insensitively(app, monkeypatch):
    """Models routinely title-case the term they echo back. Dropping those
    would mean paying for the call and storing nothing."""

    def _fake(**_kwargs):
        return {
            "translations": [{"term": "Safra Kesesi", "en": "gallbladder", "variants": []}]
        }, "raw"

    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        out = ai_service.translate_keywords(["safra kesesi"])
    assert out["safra kesesi"]["en"] == "gallbladder"


def test_overlong_translation_is_rejected(app, monkeypatch):
    """The column is String(128); a model that answers with a sentence must
    not reach the insert."""

    def _fake(**_kwargs):
        return {"translations": [{"term": "safra kesesi", "en": "x" * 200, "variants": []}]}, "raw"

    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        out = ai_service.translate_keywords(["safra kesesi"])
    assert "safra kesesi" not in out


def test_variants_are_capped_at_two(app, monkeypatch):
    def _fake(**_kwargs):
        return {
            "translations": [
                {"term": "safra kesesi", "en": "gallbladder", "variants": ["a", "b", "c", "d"]}
            ]
        }, "raw"

    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        out = ai_service.translate_keywords(["safra kesesi"])
    assert out["safra kesesi"]["variants"] == ["a", "b"]


def test_no_llm_configured_returns_lexicon_only(app, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)
    monkeypatch.setattr(ai_service, "_call_llm", _explode)
    with app.app_context():
        out = ai_service.translate_keywords(["yapay zeka", "miyokard perfüzyonu"])
    assert set(out) == {"yapay zeka"}


def test_non_dict_response_survives_the_repair_retry(app, monkeypatch):
    attempts = []

    def _fake(**kwargs):
        attempts.append(kwargs["user_msg"])
        if len(attempts) == 1:
            return None, "not json"
        return {
            "translations": [{"term": "safra kesesi", "en": "gallbladder", "variants": []}]
        }, "raw"

    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", _fake)
    with app.app_context():
        out = ai_service.translate_keywords(["safra kesesi"])
    assert len(attempts) == 2
    assert out["safra kesesi"]["en"] == "gallbladder"


# ----------------------------------------------------------------------------
# ensure_keyword_translations — persistence on the global Keyword row
# ----------------------------------------------------------------------------


def test_translations_persist_on_the_keyword_row(db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "_call_llm", _explode)
    rows = _follow(db, clean_user, "yapay zeka")

    assert ensure_keyword_translations(rows, user=clean_user) == 1
    stored = Keyword.query.filter_by(value="yapay zeka").one()
    assert stored.value_en == "artificial intelligence"
    assert stored.variants == ["AI"]
    assert stored.translated_at is not None


def test_already_translated_keywords_are_not_retranslated(db, clean_user, monkeypatch):
    rows = _follow(db, clean_user, "yapay zeka")
    ensure_keyword_translations(rows, user=clean_user)

    called = []
    monkeypatch.setattr(
        "app.modules.scrape.ai_service.translate_keywords",
        lambda keywords, **kw: called.append(keywords) or {},
    )
    assert ensure_keyword_translations(rows, user=clean_user) == 0
    assert called == []


def test_unresolvable_keyword_stays_open_for_the_next_scan(db, clean_user, monkeypatch):
    """No `translated_at` when nothing came back — the term must retry once AI
    is configured, rather than being written off with a guess."""
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)
    rows = _follow(db, clean_user, "çok niş bir terim")

    assert ensure_keyword_translations(rows, user=clean_user) == 0
    stored = Keyword.query.filter_by(value="çok niş bir terim").one()
    assert stored.value_en is None
    assert stored.translated_at is None


def test_translation_failure_never_breaks_the_scan(db, clean_user, monkeypatch):
    def _boom(keywords, **_kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.modules.scrape.ai_service.translate_keywords", _boom)
    rows = _follow(db, clean_user, "yapay zeka")
    assert ensure_keyword_translations(rows, user=clean_user) == 0


# ----------------------------------------------------------------------------
# keyword_search_terms — per-source expansion
# ----------------------------------------------------------------------------


def test_or_combining_sources_get_the_full_expansion(db, clean_user):
    (kw,) = _follow(db, clean_user, "kalp yetmezliği")
    kw.value_en = "heart failure"
    kw.variants = ["cardiac failure"]
    db.session.commit()

    terms, alias = keyword_search_terms([kw], "arxiv")
    assert terms == ["heart failure", "kalp yetmezliği", "cardiac failure"]
    assert alias["cardiac failure"] == "kalp yetmezliği"


def test_per_request_sources_get_english_only(db, clean_user):
    """Semantic Scholar issues one HTTP request per term. Expanding it would
    triple its request count against the tightest rate limit we deal with."""
    (kw,) = _follow(db, clean_user, "kalp yetmezliği")
    kw.value_en = "heart failure"
    kw.variants = ["cardiac failure"]
    db.session.commit()

    terms, alias = keyword_search_terms([kw], "semantic_scholar")
    assert terms == ["heart failure"]
    assert alias["heart failure"] == "kalp yetmezliği"


def test_untranslated_keyword_is_still_searched_as_typed(db, clean_user):
    (kw,) = _follow(db, clean_user, "transformer")
    terms, alias = keyword_search_terms([kw], "arxiv")
    assert terms == ["transformer"]
    assert alias["transformer"] == "transformer"


def test_two_interests_sharing_an_english_term_query_it_once(db, clean_user):
    a, b = _follow(db, clean_user, "yapay zeka", "yapay zekâ")
    a.value_en = b.value_en = "artificial intelligence"
    db.session.commit()

    terms, _alias = keyword_search_terms([a, b], "semantic_scholar")
    assert terms == ["artificial intelligence"]


def test_match_keyword_files_hits_under_the_users_own_term():
    terms = ["heart failure", "kalp yetmezliği"]
    alias = {"heart failure": "kalp yetmezliği", "kalp yetmezliği": "kalp yetmezliği"}
    matched = _match_keyword("Machine learning for heart failure prognosis", terms, alias)
    assert matched == "kalp yetmezliği"


def test_match_keyword_falls_back_to_the_first_term():
    terms = ["heart failure"]
    alias = {"heart failure": "kalp yetmezliği"}
    assert _match_keyword("An unrelated paper", terms, alias) == "kalp yetmezliği"


# ----------------------------------------------------------------------------
# End to end through scrape_for_user
# ----------------------------------------------------------------------------


def test_scrape_queries_the_english_term_and_attributes_it_back(db, clean_user, monkeypatch):
    (kw,) = _follow(db, clean_user, "kalp yetmezliği")
    kw.value_en = "heart failure"
    kw.translated_at = datetime.now(UTC)
    db.session.commit()

    seen: list[list[str]] = []

    def _search(keywords, *, max_results=25):  # noqa: ARG001
        seen.append(list(keywords))
        return [_payload("2401.55555", title="Deep learning for heart failure prognosis")]

    monkeypatch.setattr(
        "app.modules.scrape.service.enabled_sources",
        lambda: {"arxiv": SimpleNamespace(SOURCE_NAME="arxiv", search_for_keywords=_search)},
    )

    result = scrape_for_user(clean_user)
    assert result["linked"] == 1
    assert "heart failure" in seen[0]

    from app.modules.scrape.models import UserPaper

    link = UserPaper.query.filter_by(user_id=clean_user.id).one()
    assert link.matched_keyword == "kalp yetmezliği"
