"""WSGI entry point.

Startup order (critical — do NOT change):
  1. flask db upgrade   ← migrations must run BEFORE the app syncs modules
  2. gunicorn wsgi:app  ← only then does plugin discovery execute

In Docker this is enforced by docker/entrypoint.sh.
Running `gunicorn wsgi:app` directly without migrations will cause plugin
discovery to fail because the `modules` table won't exist yet.
"""

from app import create_app

app = create_app()
