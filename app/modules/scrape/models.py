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
    records which interest pulled it in (analytics + UI grouping).

    Per-user state: `seen_at` (opened), `is_favorite` (starred), `dismissed_at`
    (hidden from feed but still in DB for analytics). A dismissed paper can be
    un-dismissed; a favorite is a one-shot toggle.
    """

    __tablename__ = "user_papers"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    paper_id = db.Column(db.BigInteger, db.ForeignKey("papers.id"), nullable=False)
    matched_keyword = db.Column(db.String(64), nullable=True)
    seen_at = db.Column(db.DateTime(timezone=True), nullable=True)
    is_favorite = db.Column(db.Boolean, nullable=False, default=False, index=True)
    dismissed_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    paper = db.relationship("Paper", lazy="joined")
    user = db.relationship("User", backref=db.backref("paper_links", lazy="dynamic"))
    notes = db.relationship(
        "PaperNote",
        backref="user_paper",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="desc(PaperNote.created_at)",
    )

    __table_args__ = (db.UniqueConstraint("user_id", "paper_id", name="uq_user_paper"),)


class PaperNote(BaseModel):
    """Personal note attached to a UserPaper row.

    Tag is a free-form short label the user picks (e.g. "deney", "soru",
    "sonuç"). Body is plain text — markdown can come later.
    """

    __tablename__ = "paper_notes"

    user_paper_id = db.Column(
        db.BigInteger, db.ForeignKey("user_papers.id"), nullable=False, index=True
    )
    body = db.Column(db.Text, nullable=False)
    tag = db.Column(db.String(32), nullable=True)


class PaperAnalysis(BaseModel):
    """AI-generated structured analysis of a Paper — TL;DR + method/findings/
    limitations/personal-relevance breakdown. One row per (paper, target_lang)
    pair so future locales can co-exist. Refreshable but cached aggressively;
    we re-run only on explicit user request.
    """

    __tablename__ = "paper_analyses"

    paper_id = db.Column(db.BigInteger, db.ForeignKey("papers.id"), nullable=False, index=True)
    target_lang = db.Column(db.String(8), nullable=False, default="tr")
    tldr = db.Column(db.Text, nullable=True)
    method = db.Column(db.JSON, nullable=True)  # list[str] bullets
    findings = db.Column(db.JSON, nullable=True)  # list[str]
    limitations = db.Column(db.JSON, nullable=True)  # list[str]
    personal_relevance = db.Column(db.Text, nullable=True)
    model_version = db.Column(db.String(64), nullable=True)
    # Raw response payload is kept for debugging / re-parsing without re-calling
    raw_response = db.Column(db.JSON, nullable=True)

    paper = db.relationship("Paper", lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("paper_id", "target_lang", name="uq_paper_analysis_lang"),
    )


class PaperTranslation(BaseModel):
    """Title + abstract translated into a target language. Same cache logic
    as PaperAnalysis: one row per (paper, target_lang)."""

    __tablename__ = "paper_translations"

    paper_id = db.Column(db.BigInteger, db.ForeignKey("papers.id"), nullable=False, index=True)
    target_lang = db.Column(db.String(8), nullable=False)
    title_translated = db.Column(db.Text, nullable=True)
    abstract_translated = db.Column(db.Text, nullable=True)
    model_version = db.Column(db.String(64), nullable=True)

    paper = db.relationship("Paper", lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("paper_id", "target_lang", name="uq_paper_translation_lang"),
    )
