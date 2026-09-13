#!/bin/sh
# Startup order is load-bearing — see wsgi.py for explanation.
set -e

# A command passed to the container wins, and runs without migrating.
#
# This script used to ignore "$@" entirely: whatever a service asked for, it
# got `flask db upgrade` followed by gunicorn. docker-compose.prod.yml works
# around that by setting `entrypoint: []` on the worker and beat services, and
# it says so in a comment — but the workaround is on the caller's side, so the
# next service that passes `command:` and forgets the override silently becomes
# a third web server, racing the other two to run migrations.
#
# Migrations stay with the default path because the web service owns them; a
# worker that ran them too would have three containers upgrading one database
# at boot.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Step 1: run migrations (creates/alters tables before plugin discovery)
flask db upgrade

# Step 2: start the application server (plugin discovery runs inside create_app)
exec gunicorn wsgi:app \
    --bind 0.0.0.0:5000 \
    --workers 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
