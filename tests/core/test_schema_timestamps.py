"""Every created_at / updated_at column fills itself on INSERT.

BaseModel declares both with `server_default=now()`: its `onupdate` only fires
on UPDATE, so the first value has to come from the database. Three migrations
created `updated_at` without that default, and every note, analysis and
translation a migrated database stored got `updated_at = NULL` -- silently,
because the column was nullable too.

`flask db check` does not see this (server defaults are not compared; turning
that on reports seventeen harmless differences the other way). This looks at
the schema the tests run on, which in CI is the one `flask db upgrade` built.
"""

from sqlalchemy import text


def test_no_timestamp_column_lacks_a_default(db):
    rows = db.session.execute(text("""
            SELECT table_name || '.' || column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND column_name IN ('created_at', 'updated_at')
              AND column_default IS NULL
              AND table_name <> 'alembic_version'
            ORDER BY 1
            """)).scalars()
    missing = list(rows)
    assert missing == [], f"timestamp columns that INSERT leaves NULL: {missing}"
