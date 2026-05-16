#!/bin/sh
# Startup order is load-bearing — see wsgi.py for explanation.
# Step 1: run migrations (creates/alters tables before plugin discovery)
flask db upgrade

# Step 2: start the application server (plugin discovery runs inside create_app)
exec gunicorn wsgi:app \
    --bind 0.0.0.0:5000 \
    --workers 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
