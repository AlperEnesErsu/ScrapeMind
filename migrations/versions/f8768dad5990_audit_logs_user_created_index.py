"""index audit_logs by user, newest first

Revision ID: f8768dad5990
Revises: e7b204c9f83a
Create Date: 2026-09-13 11:00:00.000000

The admin audit page filters by user and orders by `created_at DESC`
(app/core/audit/routes.py). `user_id` had no index at all, so that page scanned
the whole table -- a table that only grows, and grows fastest for exactly the
accounts an admin is most likely to look up.

One composite index rather than `user_id` alone: `(user_id, created_at)` serves
the filter and the sort together, so the page reads the newest fifty rows for a
user straight off the index. Its leading column also covers the foreign-key
lookup a user deletion makes. The existing single-column `created_at` index
stays; the unfiltered page and the retention purge still use it.

Plain `create_index`, not `CONCURRENTLY`: migrations run before the web process
starts (docker/entrypoint.sh), so there is no live traffic to keep writing
while it builds. On a table large enough for that to matter, build it by hand
with CONCURRENTLY first -- this migration then finds it and is a no-op.
"""

from alembic import op

revision = "f8768dad5990"
down_revision = "e7b204c9f83a"
branch_labels = None
depends_on = None

INDEX = "ix_audit_logs_user_id_created_at"


def upgrade():
    op.execute(f"CREATE INDEX IF NOT EXISTS {INDEX} ON audit_logs (user_id, created_at)")


def downgrade():
    op.drop_index(INDEX, table_name="audit_logs")
