"""merge the patent tracking branch with the audit index

Revision ID: b1e4c7a90d2f
Revises: c3f9a17d40be, f8768dad5990
Create Date: 2026-09-13 14:20:00.000000

Two migrations landed on the same parent, `e7b204c9f83a`: this branch's
`c3f9a17d40be` (patent tracking) and #102's `f8768dad5990` (audit filter
index). Nothing conflicts between them -- they touch different tables -- but
Alembic refuses `upgrade head` with two heads, which is what CI's empty-DB
migration check hit.

A merge revision, not re-parenting either one onto the other. HANDOVER.md
section 4.9 records what re-parenting did in Faz 6: a database already
stamped at the old tip keeps its stamp, the moved migration now claims an
ancestor it never ran, and `flask db upgrade` silently skips it. That is the
exact state of any dev database that applied `c3f9a17d40be` from this branch
before merge -- re-parenting would have quietly left it without #102's index.
With a merge revision, such a database upgrades through `f8768dad5990` and
then this no-op, and nothing is skipped.

No schema change of its own.
"""

# revision identifiers, used by Alembic.
revision = "b1e4c7a90d2f"
down_revision = ("c3f9a17d40be", "f8768dad5990")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
