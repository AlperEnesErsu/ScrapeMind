"""Optional observability wiring — Sentry error tracking + Prometheus metrics.

Both are opt-in and inert by default, following the same pattern as
ANTHROPIC_API_KEY: an open-source deployment without a Sentry account or a
Prometheus stack runs exactly as before.

- **Sentry** activates only when SENTRY_DSN is set. The Flask and Celery
  integrations ship with sentry-sdk and hook themselves once init runs, so
  worker task failures land in the same project as web errors.
- **/metrics** (Prometheus text format) appears only when METRICS_ENABLED is
  true. It exposes per-endpoint request counts/latency histograms plus
  process defaults. The endpoint is unauthenticated by design — Prometheus
  scrapers don't do login flows — so in production keep it unreachable from
  outside (the prod compose only publishes the app on loopback; don't proxy
  /metrics through nginx).
"""

from __future__ import annotations

import structlog
from flask import Flask

logger = structlog.get_logger()

# Kept module-level so tests (and a future admin page) can inspect it.
metrics = None


def init_observability(app: Flask) -> None:
    _init_sentry(app)
    _init_metrics(app)


def _init_sentry(app: Flask) -> None:
    dsn = app.config.get("SENTRY_DSN")
    if not dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        # Error events always; performance tracing only if explicitly sampled.
        traces_sample_rate=float(app.config.get("SENTRY_TRACES_SAMPLE_RATE", 0.0)),
        # Don't attach request bodies/local vars — papers and notes are the
        # user's private research data and must not leak into a third party
        # beyond what a stack trace strictly needs.
        send_default_pii=False,
        environment="production" if not app.debug else "development",
    )
    logger.info("sentry_enabled")


def _init_metrics(app: Flask) -> None:
    global metrics
    if not app.config.get("METRICS_ENABLED"):
        return
    from prometheus_flask_exporter import PrometheusMetrics

    # group_by="endpoint" keeps label cardinality bounded (route names, not
    # raw paths with ids in them).
    metrics = PrometheusMetrics(app, group_by="endpoint")
    metrics.info("scrapemind_app", "ScrapeMind application info", version="1.0")
    logger.info("metrics_enabled", path="/metrics")
