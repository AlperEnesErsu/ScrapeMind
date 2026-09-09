"""Source adapter registry.

Every adapter module exposes the same contract:

  SOURCE_NAME: str
  search(query, *, max_results) -> list[PaperPayload]
  search_for_keywords(keywords, *, max_results) -> list[PaperPayload]

`enabled_sources()` reads SCRAPE_SOURCES (comma-separated names) so a
deployment can trim the list — e.g. `SCRAPE_SOURCES=arxiv` to skip the
rate-limited public APIs. Unknown names are logged and ignored.

Credential gating (Faz 5.1)
---------------------------
Every source up to Faz 4 works with no credentials at all, so "enabled" only
ever meant "the deployment listed it". Faz 5's sources (EPO OPS, PatentsView,
Scopus) do not: without a key they cannot answer, and a source that cannot
answer must not reach a scan. `scrape_for_user` marks a raising source with
its `-1` sentinel, which turns *every* nightly run into `status="partial"` for
*every* user — a permanent warning icon that means nothing.

Two independent gates keep that from happening, both declared in SOURCE_META:

  `requires_key` + `credentials_ok()`
      No credentials configured → the source never enters `enabled_sources()`
      at all. Users don't see a toggle for something that cannot run.

  `requires_admin_optin: "<system_setting_key>"`
      Credentials exist, but the deployment must still opt in (licensing —
      see `docs/adr/0002-elsevier-discovery-only.md` when 5.4 lands). Until an
      admin flips the setting the source stays off for everyone, even for a
      user who has an explicit `UserSource` row saying otherwise. Once opted
      in it is *default off* but freely toggleable — see
      `service.effective_source_prefs`.

`credentials_ok` is a callable, not a bool, because it is evaluated per call:
env vars are read at request time (`SCRAPE_SOURCES` sets the precedent), so a
deployment that adds a key does not need a restart to see the source appear.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from types import ModuleType
from typing import Any

import structlog

from app.modules.scrape.sources import (
    arxiv_source,
    bluesky_source,
    crossref_source,
    epo_ops_source,
    external_sources,
    openalex_source,
    patentsview_source,
    pubmed_source,
    rss_source,
    scopus_source,
    semantic_scholar_source,
    youtube_channel_source,
)

logger = structlog.get_logger()

AVAILABLE_SOURCES: dict[str, Any] = {
    arxiv_source.SOURCE_NAME: arxiv_source,
    semantic_scholar_source.SOURCE_NAME: semantic_scholar_source,
    pubmed_source.SOURCE_NAME: pubmed_source,
    openalex_source.SOURCE_NAME: openalex_source,
    crossref_source.SOURCE_NAME: crossref_source,
    external_sources.YOUTUBE_SOURCE_NAME: external_sources.youtube_adapter,
    external_sources.GITHUB_SOURCE_NAME: external_sources.github_adapter,
    external_sources.WEB_SOURCE_NAME: external_sources.web_adapter,
    youtube_channel_source.SOURCE_NAME: youtube_channel_source,
    epo_ops_source.SOURCE_NAME: epo_ops_source,
    patentsview_source.SOURCE_NAME: patentsview_source,
    scopus_source.SOURCE_NAME: scopus_source,
    bluesky_source.SOURCE_NAME: bluesky_source,
}
# Every RSS feed (app/modules/scrape/sources/rss_source.py:FEEDS) registers
# under its own key, sharing the one `rss_source` module — the module's
# `search_for_keywords` is a no-op (feeds are ingested globally, not via the
# per-user keyword-search flow) so this stays safe if a source loop ever
# hits it.
for _feed in rss_source.FEEDS:
    AVAILABLE_SOURCES[_feed["key"]] = rss_source

# Topic taxonomy (Faz 3 Bölüm A) — a small, easily-extended set of domain
# keys used to (a) tag every curated source and (b) classify a user's
# interests (see ai_service.classify_user_topics). Values are English msgids;
# templates wrap them in `_()` for translation — keep this dict flat so a new
# domain is a one-line addition.
TOPICS: dict[str, str] = {
    "ai": "Artificial Intelligence",
    "ml": "Machine Learning",
    "cs": "Computer Science",
    "physics": "Physics",
    "math": "Mathematics",
    "biomed": "Biomedicine",
    "social": "Social Sciences",
    "humanities": "Humanities",
    "general": "General",
}

# UI-facing metadata for each source. Kept here — not on the adapter modules —
# so adapters stay pure I/O. `label`/`desc` are English msgids; templates wrap
# them in `_()` for translation. `icon` is a bootstrap-icons class.
# `topics` (list of TOPICS keys) + `category` ("academic"|"feed"|"patent") drive
# the interest-aware source picker (Faz 3 Bölüm C) — `category` distinguishes
# the always-on-by-default databases from the topic-gated RSS feeds.
#
# Optional gating keys (Faz 5.1, see the module docstring):
#   "requires_key": True + "credentials_ok": <callable() -> bool>
#   "requires_admin_optin": "<SystemSettings key>"
# Absent keys mean "no gate", which is every source up to Faz 4.
SOURCE_META: dict[str, dict] = {
    "arxiv": {
        "label": "arXiv",
        "icon": "bi-file-earmark-text",
        "desc": "Physics, CS and mathematics preprints",
        "url": "https://arxiv.org",
        "topics": ["cs", "physics", "math", "general"],
        "category": "academic",
    },
    "semantic_scholar": {
        "label": "Semantic Scholar",
        "icon": "bi-diagram-3",
        "desc": "Multi-field corpus with citation graph",
        "url": "https://www.semanticscholar.org",
        "topics": ["general"],
        "category": "academic",
    },
    "pubmed": {
        "label": "PubMed",
        "icon": "bi-heart-pulse",
        "desc": "Biomedical and life sciences literature",
        "url": "https://pubmed.ncbi.nlm.nih.gov",
        "topics": ["biomed"],
        "category": "academic",
    },
    "openalex": {
        "label": "OpenAlex",
        "icon": "bi-globe-americas",
        "desc": "Open catalog of scholarly works across every field",
        "url": "https://openalex.org",
        "topics": [
            "ai",
            "ml",
            "cs",
            "physics",
            "math",
            "biomed",
            "social",
            "humanities",
            "general",
        ],
        "category": "academic",
    },
    "crossref": {
        "label": "Crossref",
        "icon": "bi-link-45deg",
        "desc": "DOI registry with metadata across every publisher",
        "url": "https://www.crossref.org",
        "topics": [
            "ai",
            "ml",
            "cs",
            "physics",
            "math",
            "biomed",
            "social",
            "humanities",
            "general",
        ],
        "category": "academic",
    },
    "youtube_reach": {
        "label": "YouTube Videos",
        "icon": "bi-youtube",
        "desc": "YouTube video search and metadata",
        "url": "https://youtube.com",
        "topics": ["general", "ai", "cs"],
        "category": "feed",
    },
    "github_reach": {
        "label": "GitHub Trending & Repos",
        "icon": "bi-github",
        "desc": "GitHub repository search and code trends",
        "url": "https://github.com",
        # "general" included deliberately: without it an un-classified user
        # (no LLM, or interests that classify as general) never gets GitHub
        # results by default, unlike the other two reach sources.
        "topics": ["cs", "ai", "ml", "general"],
        "category": "feed",
    },
    "web_reach": {
        "label": "Web Reader",
        "icon": "bi-globe2",
        "desc": "Web content and research article reader",
        "url": "https://r.jina.ai",
        "topics": ["general"],
        "category": "feed",
    },
    "youtube_channel": {
        "label": "YouTube Channels",
        # bi-youtube is already used by youtube_reach (video search) — a
        # distinct icon keeps the two tellable apart in the source picker.
        "icon": "bi-collection-play",
        "desc": "New uploads from channels you subscribe to, with transcripts",
        "url": "https://www.youtube.com",
        "topics": ["general", "ai", "cs"],
        "category": "feed",
    },
    # Patent sources (Faz 5.2) — the first users of 5.1's gates. Both need a
    # key *and* an admin opt-in: `patents_enabled` is registered on the system
    # settings page by `scrape.routes._register_system_toggles`.
    "epo_ops": {
        "label": "EPO Patents",
        "icon": "bi-award",
        "desc": "Worldwide patent publications, including TR (EPO OPS / DOCDB)",
        "url": "https://worldwide.espacenet.com",
        "topics": ["cs", "ai", "ml", "physics", "biomed", "general"],
        "category": "patent",
        "requires_key": True,
        "credentials_ok": epo_ops_source.credentials_ok,
        "requires_admin_optin": "patents_enabled",
    },
    "patentsview": {
        "label": "US Patents",
        "icon": "bi-patch-check",
        "desc": "US patents with inventors, assignees and CPC classes",
        "url": "https://patentsview.org",
        "topics": ["cs", "ai", "ml", "physics", "biomed", "general"],
        "category": "patent",
        "requires_key": True,
        "credentials_ok": patentsview_source.credentials_ok,
        "requires_admin_optin": "patents_enabled",
    },
    # Scopus (Faz 5.4) — discovery only, default off. Read
    # docs/adr/0002-elsevier-discovery-only.md before touching this entry.
    "scopus": {
        "label": "Scopus (discovery)",
        "icon": "bi-search",
        "desc": "Finds work the open catalogues miss; metadata comes from OpenAlex",
        "url": "https://www.scopus.com",
        "topics": ["general"],
        "category": "academic",
        "requires_key": True,
        "credentials_ok": scopus_source.credentials_ok,
        "requires_admin_optin": "scopus_enabled",
    },
    "bluesky": {
        "label": "Bluesky",
        "icon": "bi-chat-quote",
        "desc": "Academic and topical discussions from followed Bluesky accounts",
        "url": "https://bsky.app",
        "topics": ["general", "ai", "cs", "social"],
        "category": "feed",
    },
    "manual": {
        "label": "Manual",
        "icon": "bi-link-45deg",
        "desc": "Manually added link",
        "url": "",
        "topics": ["general"],
        "category": "feed",
    },
}
for _feed in rss_source.FEEDS:
    SOURCE_META[_feed["key"]] = {
        "label": _feed["label"],
        "icon": _feed["icon"],
        "desc": _feed["desc"],
        "url": _feed["url"],
        "topics": ["ai", "ml"],
        "category": "feed",
    }

# The patent sources are listed here like any other: being in SCRAPE_SOURCES
# only means "this deployment would allow it". Without EPO/PatentsView keys
# `enabled_sources()` drops them anyway, and without the `patents_enabled`
# opt-in `effective_source_prefs` keeps them off for every user.
_DEFAULT = (
    "arxiv,semantic_scholar,pubmed,openalex,crossref,youtube_reach,github_reach,web_reach,"
    "youtube_channel,epo_ops,patentsview,scopus,bluesky,"
    + ",".join(f["key"] for f in rss_source.FEEDS)
)


def credentials_ok(name: str) -> bool:
    """Whether `name`'s credentials are configured.

    True for every source that declares no `requires_key` — the Faz 0-4 sources
    need nothing, and "no gate" must never read as "gate closed". A
    `credentials_ok` callable that raises is treated as False: a probe that
    cannot answer is not evidence that the key is there.
    """
    meta = SOURCE_META.get(name, {})
    if not meta.get("requires_key"):
        return True
    probe: Callable[[], bool] | None = meta.get("credentials_ok")
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 — an unreadable probe is a missing key
        logger.warning("source_credentials_probe_failed", name=name)
        return False


def admin_optin_key(name: str) -> str | None:
    """The SystemSettings key that must be True before `name` may run, or None
    when the source needs no admin opt-in."""
    return SOURCE_META.get(name, {}).get("requires_admin_optin")


def enabled_sources() -> dict[str, ModuleType]:
    """Registry filtered by the SCRAPE_SOURCES env var (default: all), then by
    credential availability.

    A key-gated source with no key configured is dropped here rather than
    disabled downstream, so it is invisible everywhere at once — the source
    picker, `user_enabled_sources`, the library filter dropdown. The
    admin-opt-in gate deliberately does *not* apply at this level: those
    sources stay listed (an admin needs to see what they are choosing to turn
    on) and are forced off per-user in `service.effective_source_prefs`.
    """
    raw = os.getenv("SCRAPE_SOURCES", _DEFAULT)
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    out: dict[str, ModuleType] = {}
    for name in names:
        mod = AVAILABLE_SOURCES.get(name)
        if mod is None:
            logger.warning("scrape_source_unknown", name=name)
            continue
        if not credentials_ok(name):
            continue
        out[name] = mod
    return out


def source_options() -> list[dict]:
    """UI-ready list of the deployment's enabled sources, each merged with its
    display metadata. Unknown sources (no SOURCE_META row) fall back to the raw
    name as label so the UI never breaks on a new adapter."""
    out: list[dict] = []
    for name in enabled_sources():
        meta = SOURCE_META.get(name, {})
        out.append(
            {
                "name": name,
                "label": meta.get("label", name.replace("_", " ").title()),
                "icon": meta.get("icon", "bi-database"),
                "desc": meta.get("desc", ""),
                "url": meta.get("url", ""),
                "topics": meta.get("topics", []),
                "category": meta.get("category", "academic"),
                # None for everything up to Faz 4. Carried here so the picker
                # can label a source the admin has not opted into without
                # re-reading SOURCE_META itself.
                "requires_admin_optin": meta.get("requires_admin_optin"),
            }
        )
    return out


def suggested_sources(user_topics: list[str]) -> set[str]:
    """Names of the deployment's enabled sources whose `topics` intersect
    `user_topics` — the "suggested for you" set used by the source picker
    grouping and by `service.user_enabled_sources`'s topic-aware default.

    A user classified only as `["general"]` does NOT get every feed marked
    suggested — none of the curated RSS feeds carry the "general" topic (see
    SOURCE_META above), so only a *real* domain overlap counts. Broad
    academic catalogs (arXiv, Semantic Scholar) do carry "general", which is
    intentional — they're relevant regardless of specialty.
    """
    topics = {t for t in (user_topics or []) if t}
    if not topics:
        return set()
    out: set[str] = set()
    for name in enabled_sources():
        meta = SOURCE_META.get(name, {})
        src_topics = set(meta.get("topics") or [])
        if src_topics & topics:
            out.add(name)
    return out
