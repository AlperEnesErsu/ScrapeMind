"""close the drift between the models and the migrated schema

Revision ID: a7c3e9d1f2b4
Revises: f6a1b3c5d7e9
Create Date: 2026-10-11 12:00:00.000000

`flask db check` against a freshly migrated database reported 40+ operations.
Tests build their schema with `create_all()`, so they were exercising the
models' schema rather than the one production runs. Each difference was
settled on whichever side was right; this revision carries the half where the
models were right and the database was not:

- seven `created_at` / `updated_at` columns the models (BaseModel) declare
  NOT NULL were created nullable. Backfilled from the row's other timestamp,
  then constrained.
- three of those -- `updated_at` on paper_notes, paper_analyses and
  paper_translations -- also had no server default. BaseModel relies on
  `server_default=now()` for the first insert (its `onupdate` only fires on
  UPDATE), so every row those tables gained in a migrated database was
  written with `updated_at = NULL`: the unordered ORDER BY the BaseModel
  comment says the default exists to prevent. The NOT NULL constraint is
  what surfaced it -- 22 tests failed against the migrated schema. The
  default is added with the constraint.
- three indexes the models ask for and no migration created:
  `saved_searches.deleted_at`, `saved_search_notifications.deleted_at`
  (BaseModel's soft-delete filter) and `user_papers.read_later` (the
  read-later list filters on it).

The other half -- eleven `ON DELETE CASCADE` foreign keys, the notifications
user_id index and two index names -- was the database being right, and is
fixed in the models without touching the schema.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a7c3e9d1f2b4"
down_revision = "f6a1b3c5d7e9"
branch_labels = None
depends_on = None

_NOT_NULL = (
    ("paper_analyses", "updated_at"),
    ("paper_notes", "updated_at"),
    ("paper_translations", "updated_at"),
    ("saved_searches", "created_at"),
    ("saved_searches", "updated_at"),
    ("saved_search_notifications", "created_at"),
    ("saved_search_notifications", "updated_at"),
)

#: Columns created without the server default BaseModel declares.
_MISSING_DEFAULT = frozenset(
    {
        ("paper_analyses", "updated_at"),
        ("paper_notes", "updated_at"),
        ("paper_translations", "updated_at"),
    }
)

_INDEXES = (
    ("ix_saved_searches_deleted_at", "saved_searches", "deleted_at"),
    ("ix_saved_search_notifications_deleted_at", "saved_search_notifications", "deleted_at"),
    ("ix_user_papers_read_later", "user_papers", "read_later"),
)


def upgrade():
    for table, column in _NOT_NULL:
        other = "updated_at" if column == "created_at" else "created_at"
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = COALESCE({other}, now()) "  # noqa: S608
                f"WHERE {column} IS NULL"
            )
        )
        if (table, column) in _MISSING_DEFAULT:
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        else:
            op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=False)
    for name, table, column in _INDEXES:
        op.create_index(name, table, [column])


def downgrade():
    for name, table, _column in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
    for table, column in reversed(_NOT_NULL):
        if (table, column) in _MISSING_DEFAULT:
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                server_default=None,
                nullable=True,
            )
        else:
            op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=True)
