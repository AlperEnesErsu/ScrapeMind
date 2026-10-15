"""Persistence and orchestration layer for scraped papers.

The service deduplicates incoming payloads against (source, external_id)
and keeps a per-user record of which keyword surfaced what — that's how
the "For you" dashboard becomes a personal feed instead of a firehose.

This package used to be a single 3000-line `service.py`; it is split by
responsibility into the submodules below. Every name is re-exported here, so
`from app.modules.scrape.service import X` keeps working. Tests that
monkeypatch a name must patch it on the submodule that *uses* it.
"""

from __future__ import annotations

from app.modules.scrape.service.author_groups import (  # noqa: F401
    GROUP_AUTHOR_NOT_FOUND_MESSAGE,
    GROUP_CAP_MESSAGE,
    GROUP_NAME_REQUIRED_MESSAGE,
    GROUP_NAME_TAKEN_MESSAGE,
    GROUP_NOT_FOUND_MESSAGE,
    MAX_AUTHOR_GROUPS,
    MAX_GROUP_MEMBERS,
    MEMBER_CAP_MESSAGE,
    _owned_author_group,
    add_group_member,
    count_author_groups,
    count_group_members,
    create_author_group,
    delete_author_group,
    list_author_groups,
    list_group_members,
    remove_group_member,
    rename_author_group,
)
from app.modules.scrape.service.authors import (  # noqa: F401
    AUTHOR_CAP_MESSAGE,
    MAX_USER_AUTHORS,
    count_user_authors,
    follow_author,
    ingest_user_authors,
    list_user_authors,
    toggle_user_author,
    unfollow_author,
)
from app.modules.scrape.service.bluesky import (  # noqa: F401
    BLUESKY_CAP_MESSAGE,
    add_user_bluesky,
    count_user_bluesky,
    get_user_bluesky,
    ingest_user_bluesky,
    list_user_bluesky,
    remove_user_bluesky,
    toggle_user_bluesky,
)
from app.modules.scrape.service.channels import (  # noqa: F401
    CHANNEL_CAP_MESSAGE,
    add_user_channel,
    count_user_channels,
    get_user_channel,
    ingest_user_channels,
    list_user_channels,
    max_user_channels,
    remove_user_channel,
    toggle_user_channel,
)
from app.modules.scrape.service.exports import (  # noqa: F401
    papers_to_bibtex,
    papers_to_csv,
    to_bibtex,
)
from app.modules.scrape.service.feeds import (  # noqa: F401
    FEED_CAP_MESSAGE,
    FEED_LINK_CANDIDATE_LIMIT,
    _normalize_feed_url,
    add_user_feed,
    count_user_feeds,
    get_user_feed,
    ingest_user_feeds,
    link_relevant_feed_items,
    list_user_feeds,
    remove_user_feed,
    toggle_user_feed,
)
from app.modules.scrape.service.library import (  # noqa: F401
    _contains_pattern,
    _user_papers_query,
    count_user_papers,
    distinct_user_sources,
    list_user_papers,
    list_user_papers_in_window,
    search_user_papers_query,
)
from app.modules.scrape.service.locks import (  # noqa: F401
    SCRAPE_LOCK_TTL,
    _lock_client,
    _lock_key,
    acquire_scrape_lock,
    acquire_user_lock,
    release_scrape_lock,
    release_user_lock,
)
from app.modules.scrape.service.pages import (  # noqa: F401
    PAGE_CAP_MESSAGE,
    add_user_page,
    count_user_pages,
    get_user_page,
    ingest_user_pages,
    list_user_pages,
    remove_user_page,
    toggle_user_page,
)
from app.modules.scrape.service.paper_state import (  # noqa: F401
    ALLOWED_NOTE_TAGS,
    add_note,
    count_all_notes,
    delete_note,
    edit_note,
    get_note_for_user,
    get_user_paper,
    list_all_notes,
    mark_seen,
    set_dismissed,
    set_favorite,
    set_read_later,
    toggle_favorite,
    toggle_read_later,
)
from app.modules.scrape.service.papers import (  # noqa: F401
    _ENRICHABLE_FIELDS,
    _REFRESHABLE_FIELDS,
    _enrich,
    _is_empty,
    add_paper_from_url,
    link_user_paper,
    upsert_paper,
)
from app.modules.scrape.service.patents import (  # noqa: F401
    patent_sources,
    patent_sources_available,
    scrape_patents_for_user,
    search_patents_live,
)
from app.modules.scrape.service.scan_runs import (  # noqa: F401
    _COST_LLM,
    _COST_PER_FEED,
    _COST_SECONDS,
    _estimate_scan_seconds,
    _finish_scan_run,
    apply_scan_result,
    aware,
    feed_cost_estimate,
    last_scan_run,
    open_scan_run,
    purge_scan_runs,
    reap_stale_runs,
    record_scan_run,
    scan_status_context,
)
from app.modules.scrape.service.scraping import (  # noqa: F401
    _PER_KEYWORD_REQUEST_SOURCES,
    TRANSIENT_RETRY_DELAY_SECONDS,
    _hydrate_scopus_payloads,
    _match_keyword,
    _search_with_transient_retry,
    ensure_keyword_translations,
    keyword_search_terms,
    scrape_arxiv_for_user,
    scrape_for_user,
)
from app.modules.scrape.service.source_prefs import (  # noqa: F401
    admin_opted_in,
    effective_source_prefs,
    list_user_source_prefs,
    set_user_source,
    sources_card_context,
    user_enabled_sources,
)
from app.modules.scrape.service.timeline import (  # noqa: F401
    KIND_AUTHOR_SCAN,
    KIND_DISMISSED,
    KIND_FAVORITED,
    KIND_KEYWORD_ADDED,
    KIND_NOTE_ADDED,
    KIND_PATENT_SCAN,
    KIND_SCRAPE,
    TimelineEvent,
    build_timeline,
)
