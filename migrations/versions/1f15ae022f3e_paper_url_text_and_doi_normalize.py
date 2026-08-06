"""widen papers.url/pdf_url to Text, normalize existing papers.doi values

Revision ID: 1f15ae022f3e
Revises: 26cccdefc6f7
Create Date: 2026-08-06 00:00:00.000000

Two independent fixes that shipped together because both were blocking the
new OpenAlex/Crossref adapters (SCRAPING.md §10 #4 and #1):

1. `papers.url` / `papers.pdf_url` were `String(512)`. OpenAlex and Crossref
   both routinely hand back URLs (landing pages behind redirect chains,
   publisher DOI-resolver links with long query strings) that overflow that
   — the same overflow risk `external_id` already hit and was widened for in
   e5f1a2b3c4d6. Text has no such ceiling.

2. `upsert_paper` used to match a DOI with `ilike(doi.strip())`, i.e.
   case/whitespace-insensitive but otherwise whatever string a source handed
   it — "https://doi.org/10.X/Y" and "10.X/Y" never matched each other. Now
   that DOIs are normalized on write (app/modules/scrape/doi.py) and looked
   up with an exact `filter_by(doi=...)`, rows written before this change
   need their stored `doi` normalized too, or they silently stop matching
   anything new that comes in with the same DOI in canonical form.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "1f15ae022f3e"
down_revision = "26cccdefc6f7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("papers", schema=None) as batch_op:
        batch_op.alter_column(
            "url", existing_type=sa.String(length=512), type_=sa.Text(), existing_nullable=True
        )
        batch_op.alter_column(
            "pdf_url", existing_type=sa.String(length=512), type_=sa.Text(), existing_nullable=True
        )

    # Strip the common resolver prefixes and lowercase in place, mirroring
    # doi.normalize_doi's prefix list. Rows whose doi doesn't look like a DOI
    # after this are left as-is (not nulled) — best-effort cleanup only, the
    # exact-match lookup simply won't hit them until they're re-scraped.
    op.execute(r"""
        UPDATE papers
        SET doi = lower(
            regexp_replace(
                trim(doi),
                '^(https?://(dx\.)?doi\.org/|doi:)',
                '',
                'i'
            )
        )
        WHERE doi IS NOT NULL AND trim(doi) <> ''
        """)


def downgrade():
    # The doi normalization above is not reversible (the original prefixed/
    # mixed-case form is gone) — only the column type change is undone here.
    with op.batch_alter_table("papers", schema=None) as batch_op:
        batch_op.alter_column(
            "pdf_url", existing_type=sa.Text(), type_=sa.String(length=512), existing_nullable=True
        )
        batch_op.alter_column(
            "url", existing_type=sa.Text(), type_=sa.String(length=512), existing_nullable=True
        )
