"""Observability — /metrics gating and Sentry opt-in.

Neither feature may affect an unconfigured deployment: no DSN means no
Sentry init, METRICS_ENABLED=false means no /metrics route at all.

Config classes read env at import time, so tests flip app.config and call
the init helpers directly instead of fighting the environment. The metrics
app is module-scoped because prometheus_client registers collectors in a
process-global registry — a second init would raise "duplicated timeseries".
"""

import pytest

from app import create_app
from app.core.observability import _init_metrics, _init_sentry


@pytest.fixture(scope="module")
def metrics_app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["METRICS_ENABLED"] = True
    _init_metrics(app)
    return app


def test_metrics_disabled_by_default(client):
    # The shared app fixture runs with METRICS_ENABLED unset → no route.
    assert client.get("/metrics").status_code == 404


def test_metrics_endpoint_serves_prometheus_text(db, metrics_app):
    with metrics_app.test_client() as c:
        r = c.get("/metrics")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        # App info gauge + default process collectors.
        assert 'scrapemind_app{version="1.0"} 1.0' in body


def test_metrics_counts_requests_by_endpoint(db, metrics_app):
    with metrics_app.test_client() as c:
        c.get("/api/v1/health")
        body = c.get("/metrics").get_data(as_text=True)
        # Grouped by endpoint name — raw paths with ids never become labels.
        assert 'endpoint="api_v1.health"' in body


def test_sentry_skipped_without_dsn(app, monkeypatch):
    called = {}
    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: called.setdefault("init", kw))
    app.config["SENTRY_DSN"] = ""
    _init_sentry(app)
    assert "init" not in called


def test_sentry_inits_with_dsn_and_no_pii(app, monkeypatch):
    called = {}
    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: called.setdefault("init", kw))
    app.config["SENTRY_DSN"] = "https://public@example.ingest.example.com/1"
    try:
        _init_sentry(app)
    finally:
        app.config["SENTRY_DSN"] = ""
    assert "init" in called
    # Research data must not leak: PII off is part of the contract.
    assert called["init"]["send_default_pii"] is False
