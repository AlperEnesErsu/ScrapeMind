"""reports + author_groups/author_group_members, user_authors OpenAlex snapshot

Revision ID: 4360c046a92e
Revises: f4c1e8b52a76
Create Date: 2026-09-05 10:00:00.000000

Faz 6: `reports` holds one row per generated topic/author-group briefing
(mirrors `user_digests`' shape: LLM sections + an LLM-free numeric backbone).
`author_groups`/`author_group_members` let a user gather a set of followed
authors for a single report, independent of the nightly-feed `active` flag
on `user_authors` — see `app/modules/scrape/models.py` for why that is a
deliberately different promise.

The three new `user_authors` columns are an OpenAlex snapshot (institution,
works_count, cited_by_count) for the follow-candidate picker and an author
group's report header — same pattern as f4c1e8b52a76, which is why this
revises straight off it: all three are nullable, so existing rows come back
unchanged until the next resolution fills them in.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "4360c046a92e"
down_revision = "f4c1e8b52a76"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_authors", sa.Column("institution", sa.String(length=160), nullable=True)
    )
    op.add_column("user_authors", sa.Column("works_count", sa.Integer(), nullable=True))
    op.add_column("user_authors", sa.Column("cited_by_count", sa.Integer(), nullable=True))

    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("sections", sa.JSON(), nullable=True),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_reports_user_id"), "reports", ["user_id"], unique=False)
    op.create_index(op.f("ix_reports_deleted_at"), "reports", ["deleted_at"], unique=False)
    op.create_index("ix_reports_user_created", "reports", ["user_id", "created_at"], unique=False)

    op.create_table(
        "author_groups",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_author_group_name"),
    )
    op.create_index(op.f("ix_author_groups_user_id"), "author_groups", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_author_groups_deleted_at"), "author_groups", ["deleted_at"], unique=False
    )

    op.create_table(
        "author_group_members",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_author_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["author_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_author_id"], ["user_authors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_author_id", name="uq_group_member"),
    )
    op.create_index(
        op.f("ix_author_group_members_group_id"),
        "author_group_members",
        ["group_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_author_group_members_deleted_at"),
        "author_group_members",
        ["deleted_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_author_group_members_deleted_at"), table_name="author_group_members"
    )
    op.drop_index(op.f("ix_author_group_members_group_id"), table_name="author_group_members")
    op.drop_table("author_group_members")

    op.drop_index(op.f("ix_author_groups_deleted_at"), table_name="author_groups")
    op.drop_index(op.f("ix_author_groups_user_id"), table_name="author_groups")
    op.drop_table("author_groups")

    op.drop_index("ix_reports_user_created", table_name="reports")
    op.drop_index(op.f("ix_reports_deleted_at"), table_name="reports")
    op.drop_index(op.f("ix_reports_user_id"), table_name="reports")
    op.drop_table("reports")

    op.drop_column("user_authors", "cited_by_count")
    op.drop_column("user_authors", "works_count")
    op.drop_column("user_authors", "institution")
