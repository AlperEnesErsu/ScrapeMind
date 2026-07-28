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
    external_id = db.Column(
        db.String(512), nullable=False, index=True
    )  # source-local id (RSS GUIDs are full URLs)
    title = db.Column(db.Text, nullable=False)
    abstract = db.Column(db.Text, nullable=True)
    # Authors stored as JSON list of strings — simple, queryable, and good
    # enough until we want author-as-entity work in Phase 3.
    authors = db.Column(db.JSON, nullable=True)
    url = db.Column(db.String(512), nullable=True)
    pdf_url = db.Column(db.String(512), nullable=True)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    categories = db.Column(db.JSON, nullable=True)  # arXiv primary + cross-list categories
    # "news" for RSS/industry-announcement feeds; NULL is treated as "paper"
    # (every pre-Faz-2 row + all academic adapters) — no backfill needed.
    kind = db.Column(db.String(16), nullable=True)

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
    read_later = db.Column(db.Boolean, nullable=False, default=False, index=True)
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


class UserSource(BaseModel):
    """Per-user opt-out for a scrape source.

    Semantics are opt-out for backward compatibility: **no row means the source
    is enabled** for that user. A row is written only when a user changes a
    source away from its default — `enabled=False` mutes it, `enabled=True`
    re-enables it. This way existing users (who have no rows) keep scanning
    every source until they choose otherwise.
    """

    __tablename__ = "user_sources"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    source_name = db.Column(db.String(64), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (db.UniqueConstraint("user_id", "source_name", name="uq_user_source"),)


class UserFeed(BaseModel):
    """A user's own custom RSS/Atom feed (Faz 3 Bölüm D).

    Ingested per-user (see app/tasks/feed_tasks.py:link_for_user), unlike the
    curated feeds in sources/rss_source.py which are fetched once globally.
    Resulting Papers all share `source="user_feed"` regardless of which user
    added the URL or which UserFeed row it came from — two users adding the
    identical URL land on the same Paper rows via the normal
    (source, external_id) dedup, so the content is never duplicated.

    `active` is a simple per-feed pause switch (mirrors UserSource's opt-out
    toggle) — paused feeds are skipped by ingestion but the row (and its
    already-ingested papers) stay intact.
    """

    __tablename__ = "user_feeds"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    url = db.Column(db.String(512), nullable=False)
    label = db.Column(db.String(128), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)

    user = db.relationship("User", backref=db.backref("custom_feeds", lazy="dynamic"))


class ScanRun(BaseModel):
    """One recorded execution of a per-user scan.

    Exists because the UI used to *guess*: the "last update" shown on the
    dashboard was `max(UserPaper.created_at)` — a proxy that goes stale the
    moment a scan runs and finds nothing new — and the "runs every night at
    03:15" line was a hardcoded string that silently drifts from the actual
    beat schedule. A run row makes both honest, and gives us a real duration
    to show (and to estimate the next run's cost from).

    One row per (user, kind) execution, written by the Celery task itself so
    the nightly and the manual path are recorded identically — `trigger` is
    what distinguishes them.

    `details["sources"]` reuses the per-source dict `scrape_for_user` already
    returns (`{"arxiv": 12, "pubmed": -1}`, where -1 means that source errored),
    which is why `status="partial"` is simply "some value is negative".
    """

    __tablename__ = "scan_runs"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(16), nullable=False)  # "scrape" | "feeds"
    trigger = db.Column(db.String(16), nullable=False, default="auto")  # "auto" | "manual"
    # "running" | "ok" | "partial" | "skipped" | "error"
    status = db.Column(db.String(16), nullable=False, default="running")
    started_at = db.Column(db.DateTime(timezone=True), nullable=False)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    hits = db.Column(db.Integer, nullable=False, default=0)
    new_items = db.Column(db.Integer, nullable=False, default=0)
    source_count = db.Column(db.Integer, nullable=False, default=0)
    details = db.Column(db.JSON, nullable=True)

    __table_args__ = (db.Index("ix_scan_runs_user_started", "user_id", "started_at"),)

    @property
    def is_finished(self) -> bool:
        return self.finished_at is not None

    @property
    def duration_seconds(self) -> float | None:
        return None if self.duration_ms is None else self.duration_ms / 1000.0


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


class PaperChatMessage(BaseModel):
    """Sohbet mesajı modeli — kullanıcı ile Claude arasındaki RAG konuşması."""

    __tablename__ = "paper_chat_messages"

    user_paper_id = db.Column(
        db.BigInteger, db.ForeignKey("user_papers.id"), nullable=False, index=True
    )
    role = db.Column(db.String(16), nullable=False)  # "user" veya "assistant"
    content = db.Column(db.Text, nullable=False)

    user_paper = db.relationship(
        "UserPaper",
        backref=db.backref(
            "chat_messages",
            cascade="all, delete-orphan",
            lazy="select",
            order_by="PaperChatMessage.created_at",
        ),
    )


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


class UserDigest(BaseModel):
    """LLM-generated daily/weekly briefing over a user's newly-surfaced papers.

    One row per (user, period, period_start) — re-running the same window
    upserts in place, so a manual re-trigger during the same day/week is
    idempotent instead of piling up duplicate briefings.

    `highlights` is a list of `{title, why, user_paper_id}` dicts (the LLM's
    numbered "ref" index is resolved back to a real UserPaper id before this
    row is written); `themes` is a flat list of short topic strings.
    """

    __tablename__ = "user_digests"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    period = db.Column(db.String(16), nullable=False)  # "daily" | "weekly"
    period_start = db.Column(db.DateTime(timezone=True), nullable=False)
    period_end = db.Column(db.DateTime(timezone=True), nullable=False)
    summary = db.Column(db.Text, nullable=True)
    highlights = db.Column(db.JSON, nullable=True)  # [{title, why, user_paper_id}]
    themes = db.Column(db.JSON, nullable=True)  # list[str]
    item_count = db.Column(db.Integer, nullable=False, default=0)
    model_version = db.Column(db.String(64), nullable=True)
    raw_response = db.Column(db.JSON, nullable=True)

    user = db.relationship("User", backref=db.backref("digests", lazy="dynamic"))

    __table_args__ = (
        db.UniqueConstraint("user_id", "period", "period_start", name="uq_user_digest_period"),
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
