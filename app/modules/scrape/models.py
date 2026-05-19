"""Scrape result tables.

For now: papers (from arXiv, later Semantic Scholar/PubMed). Each row has a
stable `external_id` (e.g. arXiv "2401.12345v2") that we dedupe on per source.
A user_paper junction tracks which papers were surfaced for whom — that's how
the "For you" dashboard card stays per-user.
"""

from app.core.base_model import BaseModel
from app.extensions import db


class Paper(BaseModel):
    __tablename__ = "papers"

    source = db.Column(db.String(32), nullable=False, index=True)  # "arxiv", "semantic_scholar", …
    external_id = db.Column(db.String(128), nullable=False, index=True)  # source-local id
    title = db.Column(db.Text, nullable=False)
    abstract = db.Column(db.Text, nullable=True)
    # Authors stored as JSON list of strings — simple, queryable, and good
    # enough until we want author-as-entity work in Phase 3.
    authors = db.Column(db.JSON, nullable=True)
    url = db.Column(db.String(512), nullable=True)
    pdf_url = db.Column(db.String(512), nullable=True)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    categories = db.Column(db.JSON, nullable=True)  # arXiv primary + cross-list categories

    __table_args__ = (
        db.UniqueConstraint("source", "external_id", name="uq_paper_source_external"),
    )


class UserPaper(BaseModel):
    """A paper surfaced to a user by the scraper. The matched-keyword field
    records which interest pulled it in (analytics + UI grouping)."""

    __tablename__ = "user_papers"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    paper_id = db.Column(db.BigInteger, db.ForeignKey("papers.id"), nullable=False)
    matched_keyword = db.Column(db.String(64), nullable=True)
    seen_at = db.Column(db.DateTime(timezone=True), nullable=True)  # set when user opens it

    paper = db.relationship("Paper", lazy="joined")
    user = db.relationship("User", backref=db.backref("paper_links", lazy="dynamic"))

    __table_args__ = (db.UniqueConstraint("user_id", "paper_id", name="uq_user_paper"),)
