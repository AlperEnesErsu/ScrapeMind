"""Faz 1 — LLM provider abstraction, per-user key storage, digest generation,
the window query, and the daily/weekly Celery fan-out.

No real network calls: `_call_llm` / `_call_claude` / `_call_openai_compatible`
are monkeypatched throughout, same pattern as tests/modules/test_ai_service.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape import ai_service
from app.modules.scrape.models import UserDigest
from app.modules.scrape.service import link_user_paper, list_user_papers_in_window, upsert_paper
from app.modules.scrape.sources.payload import PaperPayload


def _payload(ext_id: str, *, title: str = "A Title") -> PaperPayload:
    return PaperPayload(
        source="arxiv",
        external_id=ext_id,
        title=title,
        abstract="An abstract about the paper's method and findings.",
        authors=["A. One"],
        url=f"http://arxiv.org/abs/{ext_id}",
        pdf_url=None,
        published_at=datetime(2026, 1, 15, tzinfo=UTC),
        categories=["cs.LG"],
    )


@pytest.fixture
def clean_user(db):
    for tbl in (
        "notifications",
        "user_digests",
        "paper_notes",
        "user_papers",
        "papers",
        "user_settings",
        "user_roles",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(username="digestuser").delete()
    db.session.commit()
    u = User(
        username="digestuser",
        email="digestuser@example.test",
        full_name="Digest User",
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
        "user_settings",
    ):
        db.session.execute(text(f"DELETE FROM {tbl}"))
    db.session.query(User).filter_by(id=u.id).delete()
    db.session.commit()


# ----------------------------------------------------------------------------
# Fernet round-trip + per-user key storage (never plaintext)
# ----------------------------------------------------------------------------


def test_fernet_round_trip(app):
    with app.app_context():
        enc = ai_service.encrypt_llm_key("sk-or-v1-abcdef1234567890")
        assert enc != "sk-or-v1-abcdef1234567890"
        assert ai_service.decrypt_llm_key(enc) == "sk-or-v1-abcdef1234567890"


def test_decrypt_corrupt_token_returns_none(app):
    with app.app_context():
        assert ai_service.decrypt_llm_key("not-a-valid-fernet-token") is None


def test_set_and_get_user_llm_key(app, db, clean_user):
    with app.app_context():
        ai_service.set_user_llm_key(
            clean_user, "sk-or-v1-mykey1234", "qwen/qwen-2.5-7b-instruct:free"
        )
        key, model = ai_service.get_user_llm_key(clean_user)
        assert key == "sk-or-v1-mykey1234"
        assert model == "qwen/qwen-2.5-7b-instruct:free"

        from app.core.models.settings import UserSettings

        row = UserSettings.query.filter_by(user_id=clean_user.id).first()
        assert row is not None
        assert row.settings["llm"]["api_key_enc"] != "sk-or-v1-mykey1234"


def test_set_user_llm_key_blank_keeps_existing_key(app, db, clean_user):
    """Saving just a model override (blank key field) must not wipe the
    previously stored key — the settings form supports updating either
    field independently."""
    with app.app_context():
        ai_service.set_user_llm_key(clean_user, "sk-or-v1-original", None)
        ai_service.set_user_llm_key(clean_user, None, "new-model")
        key, model = ai_service.get_user_llm_key(clean_user)
        assert key == "sk-or-v1-original"
        assert model == "new-model"


def test_clear_user_llm_key(app, db, clean_user):
    with app.app_context():
        ai_service.set_user_llm_key(clean_user, "sk-or-v1-key", None)
        assert ai_service.get_user_llm_key(clean_user)[0] == "sk-or-v1-key"
        ai_service.clear_user_llm_key(clean_user)
        assert ai_service.get_user_llm_key(clean_user) == (None, None)


def test_masked_key_preview_hides_the_key():
    assert ai_service.masked_key_preview(None) is None
    masked = ai_service.masked_key_preview("sk-or-v1-abcdefgh12345678")
    assert masked is not None
    assert "5678" in masked
    assert "abcdefgh12345678" not in masked


def test_user_llm_status_never_exposes_plaintext(app, db, clean_user):
    with app.app_context():
        ai_service.set_user_llm_key(clean_user, "sk-or-v1-secretvalue", None)
        status = ai_service.user_llm_status(clean_user)
        assert status["has_key"] is True
        assert "sk-or-v1-secretvalue" not in (status["masked"] or "")


# ----------------------------------------------------------------------------
# _resolve_llm / is_ai_enabled — provider resolution priority
# ----------------------------------------------------------------------------


def test_resolve_llm_user_key_wins_over_global(app, db, clean_user, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setitem(app.config, "OPENROUTER_API_KEY", "global-key")
    with app.app_context():
        ai_service.set_user_llm_key(clean_user, "user-key", None)
        resolved = ai_service._resolve_llm(clean_user)
        assert resolved is not None
        provider, _base_url, api_key, _model = resolved
        assert provider == "openrouter"
        assert api_key == "user-key"


def test_resolve_llm_falls_back_to_global_key(app, db, clean_user, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setitem(app.config, "OPENROUTER_API_KEY", "global-key")
    with app.app_context():
        resolved = ai_service._resolve_llm(clean_user)
        assert resolved is not None
        assert resolved[2] == "global-key"


def test_resolve_llm_none_when_no_keys_anywhere(app, db, clean_user, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setitem(app.config, "OPENROUTER_API_KEY", "")
    with app.app_context():
        assert ai_service._resolve_llm(clean_user) is None
        assert ai_service.is_ai_enabled(clean_user) is False


def test_resolve_llm_ollama_needs_no_key(app, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "ollama")
    with app.app_context():
        resolved = ai_service._resolve_llm(None)
        assert resolved is not None
        assert resolved[0] == "ollama"
        assert ai_service.is_ai_enabled(None) is True


def test_resolve_llm_anthropic_uses_global_key_only(app, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "sk-ant-test")
    with app.app_context():
        resolved = ai_service._resolve_llm(None)
        assert resolved is not None
        assert resolved[0] == "anthropic"
        assert resolved[2] == "sk-ant-test"


def test_call_llm_dispatcher_routes_to_anthropic(app, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "sk-ant-test")
    called = {}

    def _fake_claude(**kw):
        called["hit"] = True
        return {"ok": True}, "{}"

    monkeypatch.setattr(ai_service, "_call_claude", _fake_claude)
    with app.app_context():
        parsed, _raw = ai_service._call_llm(system="s", user_msg="u", max_tokens=10, user=None)
        assert called.get("hit") is True
        assert parsed == {"ok": True}


def test_call_llm_dispatcher_routes_to_openai_compatible(app, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setitem(app.config, "OPENROUTER_API_KEY", "test-key")
    called = {}

    def _fake_openai(**kw):
        called["hit"] = True
        return {"ok": True}, "{}"

    monkeypatch.setattr(ai_service, "_call_openai_compatible", _fake_openai)
    with app.app_context():
        parsed, _raw = ai_service._call_llm(system="s", user_msg="u", max_tokens=10, user=None)
        assert called.get("hit") is True
        assert parsed == {"ok": True}


def test_call_llm_dispatcher_none_when_ai_disabled(app, monkeypatch):
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setitem(app.config, "OPENROUTER_API_KEY", "")
    with app.app_context():
        parsed, raw = ai_service._call_llm(system="s", user_msg="u", max_tokens=10, user=None)
        assert parsed is None
        assert raw is None


# ----------------------------------------------------------------------------
# generate_digest
# ----------------------------------------------------------------------------

_FAKE_DIGEST = {
    "summary": "Bu pencerede ilginç bir gelişme oldu.",
    "highlights": [{"title": "Bir Makale", "why": "önemli çünkü X", "ref": 1}],
    "themes": ["transformers", "rl"],
}


def test_generate_digest_persists(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (_FAKE_DIGEST, "{}"))
    with app.app_context():
        paper = upsert_paper(_payload("2401.d0001", title="Bir Makale"))
        link, _created = link_user_paper(clean_user, paper, matched_keyword="rl")

        start = datetime.now(UTC) - timedelta(hours=24)
        end = datetime.now(UTC)
        digest = ai_service.generate_digest(
            clean_user, [link], period="daily", period_start=start, period_end=end
        )
        assert digest is not None
        assert digest.summary == _FAKE_DIGEST["summary"]
        assert digest.item_count == 1
        assert digest.themes == ["transformers", "rl"]
        assert digest.highlights[0]["user_paper_id"] == link.id
        assert UserDigest.query.filter_by(user_id=clean_user.id).count() == 1


def test_generate_digest_upserts_same_window(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (_FAKE_DIGEST, "{}"))
    with app.app_context():
        paper = upsert_paper(_payload("2401.d0002", title="Bir Makale"))
        link, _created = link_user_paper(clean_user, paper, matched_keyword="rl")
        start = datetime.now(UTC) - timedelta(hours=24)
        end = datetime.now(UTC)
        d1 = ai_service.generate_digest(
            clean_user, [link], period="daily", period_start=start, period_end=end
        )
        d2 = ai_service.generate_digest(
            clean_user, [link], period="daily", period_start=start, period_end=end
        )
        assert d1.id == d2.id
        assert UserDigest.query.filter_by(user_id=clean_user.id, period="daily").count() == 1


def test_generate_digest_disabled_returns_none(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)
    with app.app_context():
        paper = upsert_paper(_payload("2401.d0003"))
        link, _created = link_user_paper(clean_user, paper, matched_keyword="rl")
        result = ai_service.generate_digest(
            clean_user,
            [link],
            period="daily",
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC),
        )
        assert result is None
        assert UserDigest.query.filter_by(user_id=clean_user.id).count() == 0


def test_generate_digest_empty_links_no_call(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)

    def _boom(**kw):
        raise AssertionError("LLM must not be called for an empty window")

    monkeypatch.setattr(ai_service, "_call_llm", _boom)
    with app.app_context():
        result = ai_service.generate_digest(
            clean_user,
            [],
            period="daily",
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC),
        )
        assert result is None


def test_generate_digest_llm_failure_no_partial(app, db, clean_user, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (None, None))
    with app.app_context():
        paper = upsert_paper(_payload("2401.d0004"))
        link, _created = link_user_paper(clean_user, paper, matched_keyword="rl")
        result = ai_service.generate_digest(
            clean_user,
            [link],
            period="daily",
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC),
        )
        assert result is None
        assert UserDigest.query.filter_by(user_id=clean_user.id).count() == 0


def test_generate_digest_non_object_json_no_partial(app, db, clean_user, monkeypatch):
    """Weak/free models sometimes return a JSON scalar (a bare string) rather
    than the object schema. generate_digest must not crash on `.get` and must
    not persist a partial row — it retries once, then gives up returning None.
    Regression guard for a bug found in live testing (qwen3-8b)."""
    calls = {"n": 0}

    def _returns_string(**kw):
        calls["n"] += 1
        return "display_name", '"display_name"'  # valid JSON, but a str not a dict

    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", _returns_string)
    with app.app_context():
        paper = upsert_paper(_payload("2401.d0009"))
        link, _created = link_user_paper(clean_user, paper, matched_keyword="rl")
        result = ai_service.generate_digest(
            clean_user,
            [link],
            period="daily",
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC),
        )
        assert result is None
        assert UserDigest.query.filter_by(user_id=clean_user.id).count() == 0
        assert calls["n"] == 2  # one repair-retry before giving up


# ----------------------------------------------------------------------------
# list_user_papers_in_window
# ----------------------------------------------------------------------------


def test_list_user_papers_in_window_filters_by_created_at(app, db, clean_user):
    with app.app_context():
        old_paper = upsert_paper(_payload("2401.w0001", title="Old"))
        new_paper = upsert_paper(_payload("2401.w0002", title="New"))
        old_link, _ = link_user_paper(clean_user, old_paper, matched_keyword="x")
        new_link, _ = link_user_paper(clean_user, new_paper, matched_keyword="x")

        # Push the "old" link's created_at outside the window we'll query.
        old_link.created_at = datetime.now(UTC) - timedelta(days=10)
        db.session.commit()

        start = datetime.now(UTC) - timedelta(hours=24)
        end = datetime.now(UTC) + timedelta(minutes=1)
        rows = list_user_papers_in_window(clean_user, start, end)
        ids = {r.id for r in rows}
        assert new_link.id in ids
        assert old_link.id not in ids


def test_list_user_papers_in_window_excludes_dismissed(app, db, clean_user):
    with app.app_context():
        from app.modules.scrape.service import set_dismissed

        paper = upsert_paper(_payload("2401.w0003", title="Dismissed"))
        link, _ = link_user_paper(clean_user, paper, matched_keyword="x")
        set_dismissed(link, True)

        start = datetime.now(UTC) - timedelta(hours=24)
        end = datetime.now(UTC) + timedelta(minutes=1)
        rows = list_user_papers_in_window(clean_user, start, end)
        assert link.id not in {r.id for r in rows}


# ----------------------------------------------------------------------------
# digest.run_for_user (eager Celery task)
# ----------------------------------------------------------------------------


def test_digest_task_builds_digest_and_notification(app, db, clean_user, monkeypatch):
    from app.core.models.notification import Notification
    from app.tasks.digest_tasks import run_for_user

    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    monkeypatch.setattr(ai_service, "_call_llm", lambda **kw: (_FAKE_DIGEST, "{}"))
    with app.app_context():
        paper = upsert_paper(_payload("2401.t0001", title="Bir Makale"))
        link, _ = link_user_paper(clean_user, paper, matched_keyword="rl")
        # Put the link firmly inside the 24h window — creating it at the exact
        # `now` upper bound races the DB clock and can fall out of [start, end).
        link.created_at = datetime.now(UTC) - timedelta(hours=1)
        db.session.commit()

        result = run_for_user.delay(clean_user.id, "daily").get()
        assert "digest_id" in result
        assert UserDigest.query.filter_by(user_id=clean_user.id).count() == 1
        assert Notification.query.filter_by(user_id=clean_user.id).count() == 1


def test_digest_task_skips_when_no_new_items(app, db, clean_user):
    from app.tasks.digest_tasks import run_for_user

    with app.app_context():
        result = run_for_user.delay(clean_user.id, "daily").get()
        assert result == {"reason": "no_new_items"}
        assert UserDigest.query.filter_by(user_id=clean_user.id).count() == 0


def test_digest_task_missing_user_is_a_noop(app, db):
    from app.tasks.digest_tasks import run_for_user

    with app.app_context():
        result = run_for_user.delay(10_000_000, "daily").get()
        assert result == {"reason": "user_missing"}


def test_digest_fanout_queues_active_users(app, db, clean_user, monkeypatch):
    """Fan-out goes through apply_async (not delay) so every user carries a
    countdown — one LLM call per user makes bunching the worst thing we could
    do with this particular fan-out."""
    from app.tasks.digest_tasks import run_for_all_users

    calls = []
    monkeypatch.setattr(
        "app.tasks.digest_tasks.run_for_user.apply_async",
        lambda **kw: calls.append(kw),
    )
    with app.app_context():
        result = run_for_all_users("daily")
        assert result["queued"] >= 1
        assert any(c["args"] == (clean_user.id, "daily") for c in calls)
        assert all(0 <= c["countdown"] < app.config["SCAN_FANOUT_WINDOW_SECONDS"] for c in calls)


# ----------------------------------------------------------------------------
# Route level — dashboard digest card context + manual trigger + AI tab
# ----------------------------------------------------------------------------


def test_dashboard_index_renders_with_digest_context(auth_client):
    client, _uid = auth_client
    r = client.get("/")
    assert r.status_code == 200


def test_manual_digest_trigger_redirects(auth_client):
    client, _uid = auth_client
    r = client.post("/digest/run", data={"period": "daily"}, follow_redirects=False)
    assert r.status_code in (302, 303)


def test_manual_digest_trigger_htmx_returns_202(auth_client):
    client, _uid = auth_client
    r = client.post("/digest/run", data={"period": "daily"}, headers={"HX-Request": "true"})
    assert r.status_code == 202


def test_ai_tab_renders_via_profile_page(auth_client):
    client, _uid = auth_client
    r = client.get("/settings/profile?tab=ai")
    assert r.status_code == 200


def test_ai_tab_save_stores_encrypted_key_never_plaintext(auth_client):
    client, uid = auth_client
    r = client.post(
        "/papers/profile/ai/save",
        data={"openrouter_api_key": "sk-or-v1-testkey1234", "model": ""},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "sk-or-v1-testkey1234" not in body

    with client.application.app_context():
        from app.core.models.settings import UserSettings

        row = UserSettings.query.filter_by(user_id=uid).first()
        assert row is not None
        enc = row.settings["llm"]["api_key_enc"]
        assert enc != "sk-or-v1-testkey1234"
        assert ai_service.decrypt_llm_key(enc) == "sk-or-v1-testkey1234"


def test_ai_tab_displays_masked_key_only(auth_client):
    client, _uid = auth_client
    client.post(
        "/papers/profile/ai/save",
        data={"openrouter_api_key": "sk-or-v1-abcdefgh12345678", "model": ""},
    )
    r = client.get("/settings/profile/tabs/ai")
    body = r.get_data(as_text=True)
    assert "sk-or-v1-abcdefgh12345678" not in body
    assert "5678" in body


def test_ai_tab_clear_removes_key(auth_client):
    client, uid = auth_client
    client.post(
        "/papers/profile/ai/save",
        data={"openrouter_api_key": "sk-or-v1-clearme123", "model": ""},
    )
    r = client.post("/papers/profile/ai/clear")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "sk-or-v1-clearme123" not in body

    with client.application.app_context():
        from app.core.models.settings import UserSettings

        row = UserSettings.query.filter_by(user_id=uid).first()
        assert "llm" not in (row.settings or {})
