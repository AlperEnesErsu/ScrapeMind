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
    # Text, not String(512): OpenAlex/Crossref URLs (redirect chains, long
    # DOI-resolver query strings) routinely overflow a 512-char varchar —
    # same overflow class external_id was widened for (see
    # e5f1a2b3c4d6_widen_paper_external_id).
    url = db.Column(db.Text, nullable=True)
    pdf_url = db.Column(db.Text, nullable=True)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    categories = db.Column(db.JSON, nullable=True)  # arXiv primary + cross-list categories
    doi = db.Column(db.String(128), nullable=True, index=True)
    # "news" for RSS/industry-announcement feeds; NULL is treated as "paper"
    # (every pre-Faz-2 row + all academic adapters) — no backfill needed.
    kind = db.Column(db.String(16), nullable=True)

    # --- Journal quality signals (Faz 5.3) ---
    # Linking ISSN, the identifier that survives a journal changing title or
    # splitting print/online ISSNs. Deliberately a plain indexed column and
    # *not* a foreign key to `journals`: a paper's ISSN is real whether or not
    # the Scimago seed happens to know that journal, and an FK would either
    # reject the paper or force a placeholder row for every unknown ISSN.
    issn_l = db.Column(db.String(9), nullable=True, index=True)
    # Citations reported by whichever source last enriched this row. Unlike
    # every other enrichable field this one is *refreshed* rather than
    # fill-only — see `_REFRESHABLE_FIELDS` in service.py.
    cited_by_count = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("source", "external_id", name="uq_paper_source_external"),
    )

    #: The `journals` row for this paper's ISSN, when one has been seeded.
    #:
    #: `viewonly` and hand-written join condition because there is no foreign
    #: key here (see `issn_l` above) — SQLAlchemy needs to be told explicitly
    #: which side is "foreign". Nothing writes through this relationship: the
    #: journals table is populated only by `scripts/seed_journals.py`.
    #:
    #: Left lazy on purpose. Most queries never touch it; the two feed paths
    #: that render a quartile badge over ~100 cards ask for it explicitly with
    #: `joinedload`, the same way they do for `video_summary`.
    journal = db.relationship(
        "Journal",
        primaryjoin="foreign(Paper.issn_l) == remote(Journal.issn_l)",
        viewonly=True,
        uselist=False,
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
    etag = db.Column(db.String(256), nullable=True)
    last_modified = db.Column(db.String(256), nullable=True)

    user = db.relationship("User", backref=db.backref("custom_feeds", lazy="dynamic"))


class UserChannel(BaseModel):
    """A user's YouTube channel subscription (Faz 3 — agent reach).

    Deliberately its own table rather than a `kind` column on `UserFeed`,
    for three reasons: the cap is counted separately from `MAX_USER_FEEDS`
    (an admin-set `max_user_channels` system setting — see
    `service.max_user_channels`, distinct from the per-feed cap because each
    channel is a heavier per-night cost); it needs a `channel_id` column
    `UserFeed` has no use for; and its ingestion path (added in a later
    commit) chains transcript fetching and AI summarization that RSS feeds
    never touch, so the two are governed by unrelated code paths even though
    they share the "user subscribes to a recurring source" shape.

    `active` mirrors `UserFeed.active` — a pause switch, not a delete.
    `etag`/`last_modified` back the same conditional-GET machinery as
    `UserFeed` (see `sources/youtube_channel_source.fetch_channel_videos`).
    `last_video_at` is set by the ingestion task (not this commit) so the UI
    can show "last new video" without querying Papers.
    """

    __tablename__ = "user_channels"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    channel_id = db.Column(db.String(64), nullable=False)  # the UC... id
    title = db.Column(db.String(200), nullable=True)
    url = db.Column(db.String(512), nullable=False)  # canonical channel URL
    active = db.Column(db.Boolean, nullable=False, default=True)
    etag = db.Column(db.String(256), nullable=True)
    last_modified = db.Column(db.String(256), nullable=True)
    last_video_at = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship("User", backref=db.backref("youtube_channels", lazy="dynamic"))

    __table_args__ = (db.UniqueConstraint("user_id", "channel_id", name="uq_user_channel"),)


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
    # "scrape" | "feeds" | "channels" | "patents" | "authors". String(16) with
    # no constraint, so a new kind needs no migration — but
    # `scan_status_context` and `library/_timeline.html` branch on it, so they
    # do need updating.
    kind = db.Column(db.String(16), nullable=False)
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


# Junction: which papers sit in which collection. Points at user_papers (not
# papers) so membership is inherently the owner's — a collection can only ever
# hold the owner's own library rows.
collection_papers = db.Table(
    "collection_papers",
    db.Column(
        "collection_id",
        db.BigInteger,
        db.ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "user_paper_id",
        db.BigInteger,
        db.ForeignKey("user_papers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Collection(BaseModel):
    """A user-named folder of papers — organise a library by project/topic,
    beyond the single favorites/read-later flags.

    Membership is via the collection_papers junction to UserPaper, so a
    collection only ever contains the owner's own papers.
    """

    __tablename__ = "collections"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    share_token = db.Column(db.String(64), nullable=True, unique=True, index=True)
    is_public = db.Column(db.Boolean, nullable=False, default=False)

    papers = db.relationship(
        "UserPaper",
        secondary=collection_papers,
        lazy="select",
        order_by="desc(UserPaper.created_at)",
    )

    __table_args__ = (db.UniqueConstraint("user_id", "name", name="uq_collection_user_name"),)


class UserAuthor(BaseModel):
    """An author a user follows, to be told when they publish something new.

    The table has existed since PR #4-#6 but nothing ever read it; Faz 5.4 is
    what makes it load-bearing. Extended rather than replaced — the unique
    constraint on `(user_id, author_name)` and any rows already written stay
    valid.

    Why both `orcid` and `openalex_id`. ORCID is what a researcher knows and
    types; OpenAlex is what the API queries by. Resolution happens once, on
    follow (`openalex_source.fetch_author`), so the nightly run is a plain
    id lookup rather than a resolution per author per night. A row with no
    `openalex_id` is a follow we could not resolve — kept, because the name is
    still what the user asked for, but skipped by ingestion.

    `last_work_at` is the high-water mark of what we have already ingested for
    this author. Nightly runs ask OpenAlex only for works published after it,
    which is what keeps a prolific author from re-importing a career's output
    every night.
    """

    __tablename__ = "user_authors"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    author_name = db.Column(db.String(128), nullable=False)

    # "A5023888391" — OpenAlex's author id, without the URL prefix.
    openalex_id = db.Column(db.String(32), nullable=True, index=True)
    # "0000-0002-1825-0097" — 19 chars including hyphens.
    orcid = db.Column(db.String(19), nullable=True)
    last_work_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Pause switch, mirroring UserFeed/UserChannel: a paused follow keeps its
    # row (and its high-water mark) instead of losing both to a delete.
    active = db.Column(db.Boolean, nullable=False, default=True, server_default="true")

    user = db.relationship("User", backref=db.backref("followed_authors", lazy="dynamic"))

    __table_args__ = (db.UniqueConstraint("user_id", "author_name", name="uq_user_author"),)


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


class VideoSummary(BaseModel):
    """LLM-generated TL;DR + highlights/topics for one channel video's
    transcript. One row per Paper — see `app/modules/scrape/ai_service.py`
    `generate_video_summary` for the generation path.

    Two decisions worth documenting:

    1. Unique on `paper_id` alone, unlike `PaperAnalysis` which is unique on
       `(paper_id, target_lang)`. The feed renders up to 100 cards, and a
       `uselist=False` relationship collapses to one `joinedload` LEFT JOIN
       instead of an N+1. The cost is no per-language cache: a video gets
       one summary, in the deployment's default language.
    2. The raw transcript is never stored here — only `transcript_chars`.
       `docs/SCRAPING.md` §11 commits to never republishing copyrighted
       content (summary + link back only), and transcripts are also large;
       keeping only the character count is enough to show "based on an
       11,000-character transcript" without holding onto the text itself.
    """

    __tablename__ = "video_summaries"

    paper_id = db.Column(
        db.BigInteger, db.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    tldr = db.Column(db.Text, nullable=True)
    highlights = db.Column(db.JSON, nullable=True)  # list[str]
    topics = db.Column(db.JSON, nullable=True)  # list[str]
    transcript_chars = db.Column(db.Integer, nullable=True)
    source_lang = db.Column(db.String(8), nullable=True)  # transcript language actually used
    target_lang = db.Column(db.String(8), nullable=True)  # summary language
    model_version = db.Column(db.String(64), nullable=True)
    raw_response = db.Column(db.JSON, nullable=True)

    paper = db.relationship("Paper", backref=db.backref("video_summary", uselist=False))


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


class Journal(BaseModel):
    """Journal quality signals, seeded from Scimago + DOAJ (Faz 5.3).

    This is the part of Scopus/WoS that actually mattered for Turkish
    academia — "is this a Q1 journal" decides promotion and incentive
    payments — obtained without their licence. See `docs/PHASE5.md` §2 for why
    the licensed route was rejected and `scripts/seed_journals.py` for the
    import.

    Keyed on the **linking ISSN** (`issn_l`), not the title: journals get
    renamed, merged and split, and a print/online ISSN pair is one journal.
    Papers carry the same column and join on it — no foreign key, because a
    paper's ISSN is a fact independent of whether this table has heard of that
    journal (see `Paper.issn_l`).

    `sjr_year` is stored because the Scimago ranking is an annual snapshot: a
    row reading "Q1" means "Q1 in that year", and the UI has to be able to say
    which year it is showing rather than implying a timeless verdict.

    **Attribution is a licence condition**, not a courtesy — SJR data is
    CC BY-NC. Anywhere a quartile is displayed, Scimago must be credited (see
    `docs/SCRAPING.md` §11). The NC clause is also a live constraint: it is
    fine while ScrapeMind is non-commercial, and would need revisiting if that
    ever changes.
    """

    __tablename__ = "journals"

    issn_l = db.Column(db.String(9), nullable=False, index=True, unique=True)
    title = db.Column(db.Text, nullable=False)
    publisher = db.Column(db.Text, nullable=True)
    # Numeric, not Float: SJR values are published to three decimals and are
    # compared/ordered in the UI, so exact decimal storage avoids a 0.1 + 0.2
    # class of surprise in a number users read as authoritative.
    sjr = db.Column(db.Numeric(10, 3), nullable=True)
    sjr_quartile = db.Column(db.String(2), nullable=True, index=True)  # "Q1".."Q4"
    sjr_year = db.Column(db.Integer, nullable=True)
    h_index = db.Column(db.Integer, nullable=True)
    is_doaj = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    is_oa = db.Column(db.Boolean, nullable=False, default=False, server_default="false")


class SourceQuotaUsage(BaseModel):
    """Cumulative weekly consumption of one licensed/metered source's quota
    (Faz 5.1).

    Why this is not `ratelimit.py`'s Redis counter. That counter is a
    fixed-window *rate* limiter — "8 requests per second" — and it is
    deliberately **fail-open**: when Redis is unreachable the request goes
    through, because a missing limiter should degrade to the old behaviour
    rather than stop every scan. Neither property survives contact with a
    published quota:

      * The windows are weeks, not seconds (Scopus 20.000 requests/week, EPO
        OPS 4 GB/week). A Redis key that must live seven days is a durability
        promise Redis is not being asked to make here — it is configured as a
        broker, and a restart or eviction silently resets the budget to zero
        used.
      * Fail-open is the wrong direction for a contract. Blowing through a
        licensed weekly quota because a cache was down is a worse outcome than
        skipping a nightly run, so `ratelimit.consume_quota` fails **closed**.

    The two coexist and are called together: `<name>_slot()` shapes the
    instantaneous rate, `consume_quota()` spends from the cumulative budget.

    `bytes_used` exists because EPO OPS meters bandwidth rather than calls;
    sources that meter calls simply leave it at 0.
    """

    __tablename__ = "source_quota_usage"

    source_name = db.Column(db.String(64), nullable=False)
    # Start of the quota week, UTC-normalised to Monday 00:00 by
    # `ratelimit.quota_window_start`. Stored (not derived at query time) so the
    # unique constraint can do the "one row per source per week" work.
    window_start = db.Column(db.DateTime(timezone=True), nullable=False)
    requests_used = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    bytes_used = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")

    __table_args__ = (
        db.UniqueConstraint("source_name", "window_start", name="uq_source_quota_window"),
    )
