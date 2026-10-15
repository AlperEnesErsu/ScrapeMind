"""Papers UI — the user-facing read view + per-paper actions.

Route layout:
    /papers/                      → Discover feed (default view)
    /papers/?view=favorites       → starred only
    /papers/?view=dismissed       → hidden bin (recovery)
    /papers/<id>                  → paper detail (read + notes panel)
    /papers/<id>/open             → mark seen and redirect to source URL
    /papers/<id>/favorite/toggle  → HTMX: flip star, swap card actions
    /papers/<id>/dismiss          → HTMX: hide from feed, swap to undo banner
    /papers/<id>/undismiss        → HTMX: put back
    /papers/<id>/notes            → HTMX: POST adds, GET lists (partial)
    /papers/notes/<note_id>       → HTMX: DELETE drops a note
    /papers/run                   → POST queue a one-off scrape
"""

from __future__ import annotations

import structlog
from flask import Blueprint

logger = structlog.get_logger()

scrape_bp = Blueprint("scrape", __name__, template_folder="templates", static_folder="static")


@scrape_bp.app_context_processor
def _inject_source_meta():
    """Make SOURCE_META/TOPICS available to every template app-wide —
    `_paper_card.html`/`_sources_card.html` are included from
    dashboard/for_you.html and library/index.html too, not just routes owned
    by this blueprint, so `app_context_processor` (not `context_processor`)
    is required here."""
    from app.modules.scrape.sources import SOURCE_META, TOPICS

    return {"source_meta": SOURCE_META, "topics_meta": TOPICS}


# ----------------------------------------------------------------------------
# Handlers live in app/modules/scrape/views/ and register on `scrape_bp` when
# imported here; they read helpers through `routes.<name>` at call time, so
# patching a name on this module still works. Keep these imports at the bottom
# (circular import).
# ----------------------------------------------------------------------------
from app.modules.scrape.views.authors import (  # noqa: E402, F401
    _owned_group_or_none,
    _render_authors_tab,
    submit_author_add_to_group,
    submit_author_delete,
    submit_author_follow,
    submit_author_pause,
    submit_author_search,
    submit_group_create,
    submit_group_delete,
    submit_group_member_add,
    submit_group_member_remove,
    submit_group_rename,
    submit_group_report,
)
from app.modules.scrape.views.paper_actions import (  # noqa: E402, F401
    add_note_route,
    bulk_action,
    cite_route,
    delete_note_route,
    dismiss,
    edit_note_route,
    export_notes_route,
    open_paper,
    toggle_favorite_route,
    toggle_read_later_route,
    undismiss,
    view_note_route,
)
from app.modules.scrape.views.papers import (  # noqa: E402, F401
    _get_external_similar,
    _get_internal_similar,
    _is_htmx,
    _render_card,
    _widget_ctx,
    add_citation_paper,
    add_link_route,
    citation_graph,
    detail,
    feed,
    generate_analysis_route,
    generate_translation_route,
    generate_video_summary_route,
    send_chat_message,
    similar,
)
from app.modules.scrape.views.prior_art import (  # noqa: E402, F401
    _PRIOR_ART_MAX_TERMS,
    _PRIOR_ART_STOPWORDS,
    _prior_art_terms,
    prior_art,
)
from app.modules.scrape.views.reports import (  # noqa: E402, F401
    _report_create_form,
    report_create,
    report_delete,
    report_detail,
    report_status,
    reports,
)
from app.modules.scrape.views.scan import (  # noqa: E402, F401
    _QUEUE_WAIT_LIMIT,
    _celery_task_finished,
    run_now,
    scan_status,
    scrape_status_poll,
)
from app.modules.scrape.views.settings_tabs import (  # noqa: E402, F401
    _ai_ctx,
    _authors_ctx,
    _bluesky_list_ctx,
    _channel_list_ctx,
    _feed_list_ctx,
    _page_list_ctx,
    _register_system_toggles,
    _register_tabs,
    _render_settings_tab,
    _render_source_manager_result,
    _source_manager_ctx,
    source_manager,
    submit_ai_clear,
    submit_ai_save,
)
from app.modules.scrape.views.subscriptions import (  # noqa: E402, F401
    bluesky_list,
    channel_list,
    feed_list,
    page_list,
    submit_bluesky_add,
    submit_bluesky_remove,
    submit_bluesky_toggle,
    submit_channel_add,
    submit_channel_remove,
    submit_channel_toggle,
    submit_feed_add,
    submit_feed_remove,
    submit_feed_toggle,
    submit_page_add,
    submit_page_remove,
    submit_page_toggle,
)

# Side-effect: registers the "AI Settings" profile tab and the deployment-level
# source toggles. Runs when this module is imported (app/__init__.py imports
# scrape_bp from here at startup).
_register_tabs()
_register_system_toggles()
