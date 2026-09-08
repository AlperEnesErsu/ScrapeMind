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

import time

import structlog
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape.forms import (
    AiSettingsForm,
    FollowAuthorForm,
    UserBlueskyForm,
    UserChannelForm,
    UserFeedForm,
    UserPageForm,
)
from app.modules.scrape.service import (
    add_note,
    count_user_papers,
    delete_note,
    edit_note,
    get_note_for_user,
    get_user_paper,
    list_user_papers,
    mark_seen,
    set_dismissed,
    to_bibtex,
    toggle_favorite,
)

logger = structlog.get_logger()

scrape_bp = Blueprint("scrape", __name__, template_folder="templates")


@scrape_bp.app_context_processor
def _inject_source_meta():
    """Make SOURCE_META/TOPICS available to every template app-wide —
    `_paper_card.html`/`_sources_card.html` are included from
    dashboard/for_you.html and library/index.html too, not just routes owned
    by this blueprint, so `app_context_processor` (not `context_processor`)
    is required here."""
    from app.modules.scrape.sources import SOURCE_META, TOPICS

    return {"source_meta": SOURCE_META, "topics_meta": TOPICS}


# ------------------------------------------------------------------ #
# AI settings profile tab (per-user OpenRouter key)
# ------------------------------------------------------------------ #


def _feed_list_ctx(filter_: str = "all") -> dict:
    """Context for the `settings/_feed_list.html` partial.

    Deliberately free of `classify_user_topics` / `user_llm_status`: this is
    what a toggle or delete re-renders, and those two are expensive (the first
    is an LLM call). `_ai_ctx` adds them for the full-tab render only.
    """
    from flask import current_app

    from app.modules.scrape.service import feed_cost_estimate, list_user_feeds

    feeds = list_user_feeds(current_user)
    if filter_ == "active":
        shown = [f for f in feeds if f.active]
    elif filter_ == "paused":
        shown = [f for f in feeds if not f.active]
    else:
        filter_ = "all"
        shown = feeds

    active_count = sum(1 for f in feeds if f.active)
    return {
        "user_feeds": shown,
        "feed_count": len(feeds),
        "active_feed_count": active_count,
        "filter": filter_,
        "max_user_feeds": current_app.config.get("MAX_USER_FEEDS", 50),
        # Feed-only cost, so the panel never resolves source prefs (see
        # service.feed_cost_estimate).
        "feed_cost_delta": feed_cost_estimate(active_count),
    }


def _channel_list_ctx(filter_: str = "all") -> dict:
    """Context for the `settings/_channel_list.html` partial.

    Same reasoning as `_feed_list_ctx`: this is what a toggle or delete
    re-renders, so it stays free of `classify_user_topics` / `user_llm_status`
    — `_ai_ctx` adds those for the full-tab render only.
    """
    from app.modules.scrape.service import list_user_channels, max_user_channels

    channels = list_user_channels(current_user)
    if filter_ == "active":
        shown = [c for c in channels if c.active]
    elif filter_ == "paused":
        shown = [c for c in channels if not c.active]
    else:
        filter_ = "all"
        shown = channels

    return {
        "user_channels": shown,
        "channel_count": len(channels),
        "active_channel_count": sum(1 for c in channels if c.active),
        "channel_filter": filter_,
        "max_user_channels": max_user_channels(),
    }


def _page_list_ctx(filter_: str = "all") -> dict:
    """Context for the `settings/_page_list.html` partial."""
    from flask import current_app

    from app.modules.scrape.service import list_user_pages

    pages = list_user_pages(current_user)
    if filter_ == "active":
        shown = [p for p in pages if p.active]
    elif filter_ == "paused":
        shown = [p for p in pages if not p.active]
    else:
        filter_ = "all"
        shown = pages

    return {
        "user_pages": shown,
        "page_count": len(pages),
        "active_page_count": sum(1 for p in pages if p.active),
        "page_filter": filter_,
        "max_user_pages": current_app.config.get("MAX_USER_PAGES", 30),
    }


def _bluesky_list_ctx(filter_: str = "all") -> dict:
    """Context for the `settings/_bluesky_list.html` partial."""
    from flask import current_app

    from app.modules.scrape.service import list_user_bluesky

    accounts = list_user_bluesky(current_user)
    if filter_ == "active":
        shown = [a for a in accounts if a.active]
    elif filter_ == "paused":
        shown = [a for a in accounts if not a.active]
    else:
        filter_ = "all"
        shown = accounts

    return {
        "user_bluesky": shown,
        "bluesky_count": len(accounts),
        "active_bluesky_count": sum(1 for a in accounts if a.active),
        "bluesky_filter": filter_,
        "max_user_bluesky": current_app.config.get("MAX_USER_BLUESKY", 20),
    }


def _source_manager_ctx(*, clear_forms: bool = False) -> dict:
    """Context for `settings/_source_manager.html` — the add-forms plus
    the feed/channel/page/bluesky list contexts.
    """
    kwargs = {"formdata": None} if clear_forms else {}
    return {
        "feed_form": UserFeedForm(**kwargs),
        "channel_form": UserChannelForm(**kwargs),
        "page_form": UserPageForm(**kwargs),
        "bluesky_form": UserBlueskyForm(**kwargs),
        **_feed_list_ctx(),
        **_channel_list_ctx(),
        **_page_list_ctx(),
        **_bluesky_list_ctx(),
    }


def _ai_ctx(*, clear_forms: bool = False):
    from flask import current_app

    from app.modules.scrape.ai_service import classify_user_topics, user_llm_status

    return {
        "form": AiSettingsForm(),
        "status": user_llm_status(current_user),
        "provider": (current_app.config.get("LLM_PROVIDER") or "openrouter").strip().lower(),
        "default_model": current_app.config.get("OPENROUTER_MODEL"),
        "user_topics": classify_user_topics(current_user),
        **_source_manager_ctx(clear_forms=clear_forms),
    }


def _authors_ctx():
    """Context for the "Followed Authors" profile tab (Faz 5.4).

    Surfaces the user's own ORCID from the Identifiers tab so following your
    own publications is one click rather than a copy-paste between two
    screens. Reading it here (rather than putting this UI inside the academic
    module) keeps the data's owner and its UI in the same module — scrape owns
    `UserAuthor`, and scrape already imports `academic.service` for keywords.
    """
    from app.modules.academic.service import list_user_identifiers
    from app.modules.scrape.service import MAX_USER_AUTHORS, list_user_authors

    own = [i.value for i in list_user_identifiers(current_user, type_code="orcid")]
    authors = list_user_authors(current_user)
    followed_orcids = {a.orcid for a in authors if a.orcid}
    return {
        "form": FollowAuthorForm(),
        "authors": authors,
        "author_count": len(authors),
        "max_user_authors": MAX_USER_AUTHORS,
        # Only ORCIDs not already followed — offering "follow yourself" to
        # someone who already does is noise.
        "own_orcids": [o for o in own if o not in followed_orcids],
    }


def _register_tabs():
    """Tab registry'ye scrape modülünün AI Settings ve Followed Authors
    tablarını ekle — uygulama başlarken (bu modül import edildiğinde)
    çağrılır."""
    from app.core.settings.tab_registry import register_profile_tab

    register_profile_tab("ai", "bi-robot", "AI Settings", _ai_ctx)
    register_profile_tab("authors", "bi-person-badge", "Followed Authors", _authors_ctx)


def _register_system_toggles():
    """Register scrape's deployment-level source switches on the core system
    settings page (Faz 5.1).

    The switches live here, not in `SystemSettingsForm`, because what they mean
    — which APIs they gate, which env vars back them — is scrape's business and
    `app/core/` never imports from `app/modules/` (CLAUDE.md rule 1).

    The `credentials_ok` probes read env directly, matching the convention
    `SCRAPE_SOURCES` / `SEMANTIC_SCHOLAR_API_KEY` already use: these are
    secrets, and `SystemSettings.value` is plain JSON in a table admins can
    read, so no key is ever stored there — only the boolean.
    """
    import os

    from app.core.settings.toggle_registry import register_system_toggle

    register_system_toggle(
        "patents_enabled",
        label="Patent sources",
        help_text="Scan EPO OPS and PatentsView for patents matching user interests.",
        credentials_ok=lambda: bool(os.getenv("EPO_OPS_KEY") and os.getenv("EPO_OPS_SECRET"))
        or bool(os.getenv("PATENTSVIEW_API_KEY")),
        missing_credentials_hint="Set EPO_OPS_KEY + EPO_OPS_SECRET or PATENTSVIEW_API_KEY.",
    )
    register_system_toggle(
        "scopus_enabled",
        label="Scopus (discovery only)",
        help_text=(
            "Off by default. Scopus keys are bound to an institution IP and its "
            "licence forbids storing abstracts — see docs/PHASE5.md."
        ),
        credentials_ok=lambda: bool(os.getenv("SCOPUS_API_KEY")),
        missing_credentials_hint="Set SCOPUS_API_KEY (and SCOPUS_INSTTOKEN off campus).",
    )


def _render_settings_tab(tab: str, **ctx):
    return render_template(f"settings/_tab_{tab}.html", active_tab=tab, **ctx)


@scrape_bp.route("/profile/source-manager", methods=["GET"])
@login_required
def source_manager():
    """Lazy-loaded body of the home-page 'manage your sources' modal.

    The card renders on every home-page load and most loads never open the
    modal, so this must stay cheap: built from `_source_manager_ctx()` only
    (feed/channel lists + the two add-forms), never `_ai_ctx()` — see
    `_feed_list_ctx`'s docstring for why that one is off-limits here
    (`classify_user_topics` is an LLM round trip).
    """
    return render_template(
        "settings/_source_manager.html", surface="modal", **_source_manager_ctx()
    )


def _render_source_manager_result(
    surface: str | None, *, active_pane: str = "feeds", added: bool = False, **flash_kwargs
):
    """Shared response for submit_feed_add/submit_channel_add: the modal
    surface swaps just the source-manager partial (cheap context); anything
    else (the settings tab, or no `surface` field at all) re-renders the
    full AI tab, unchanged from before this route became surface-aware.

    `active_pane` ("feeds" | "channels") keeps the modal on the tab the user
    was just working in — re-rendering the whole partial after an add would
    otherwise reset to the first tab, which is exactly the kind of thing that
    makes a popup feel broken (add a channel, get bounced back to RSS). The
    settings tab has no tab strip and simply ignores the value — passed
    through anyway so both branches share one signature.

    `added` says the add succeeded, which clears the inputs (see
    `_source_manager_ctx`). On failure they keep their values so the user can
    fix the URL instead of retyping it.
    """
    if surface == "modal":
        return render_template(
            "settings/_source_manager.html",
            surface="modal",
            active_pane=active_pane,
            **_source_manager_ctx(clear_forms=added),
            **flash_kwargs,
        )
    return _render_settings_tab(
        "ai", active_pane=active_pane, **flash_kwargs, **_ai_ctx(clear_forms=added)
    )


@scrape_bp.route("/profile/ai/save", methods=["POST"])
@login_required
def submit_ai_save():
    from app.modules.scrape.ai_service import set_user_llm_key

    form = AiSettingsForm()
    if form.validate_on_submit():
        key = (form.openrouter_api_key.data or "").strip()
        model = (form.model.data or "").strip()
        if not key and not model:
            return _render_settings_tab(
                "ai",
                flash_msg=_("Nothing to save — enter a key or a model override."),
                flash_kind="warning",
                **_ai_ctx(),
            )
        set_user_llm_key(current_user, key or None, model or None)
        log_action(
            "user.llm_key_updated",
            entity_type="user",
            entity_id=current_user.id,
            changes={"key_updated": bool(key), "model": model or None},
        )
        return _render_settings_tab(
            "ai", flash_msg=_("AI settings saved."), flash_kind="success", **_ai_ctx()
        )
    return _render_settings_tab(
        "ai",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
        **_ai_ctx(),
    )


@scrape_bp.route("/profile/ai/clear", methods=["POST"])
@login_required
def submit_ai_clear():
    from app.modules.scrape.ai_service import clear_user_llm_key

    clear_user_llm_key(current_user)
    log_action("user.llm_key_cleared", entity_type="user", entity_id=current_user.id)
    return _render_settings_tab(
        "ai", flash_msg=_("Your API key was removed."), flash_kind="success", **_ai_ctx()
    )


# ------------------------------------------------------------------ #
# Custom RSS feeds (Faz 3 Bölüm D) — same AI/Kaynaklar profile tab
# ------------------------------------------------------------------ #


@scrape_bp.route("/profile/feeds/add", methods=["POST"])
@login_required
def submit_feed_add():
    from app.modules.scrape.service import add_user_feed

    surface = request.form.get("surface")
    form = UserFeedForm()
    if form.validate_on_submit():
        feed, err = add_user_feed(current_user, form.url.data, form.label.data)
        if feed is not None:
            log_action(
                "user.feed_added",
                entity_type="user_feed",
                entity_id=str(feed.id),
                changes={"url": feed.url},
            )
            return _render_source_manager_result(
                surface,
                active_pane="feeds",
                added=True,
                flash_msg=_("Feed added."),
                flash_kind="success",
            )
        return _render_source_manager_result(
            surface,
            active_pane="feeds",
            flash_msg=_(err or "Could not add that feed."),
            flash_kind="danger",
        )
    return _render_source_manager_result(
        surface,
        active_pane="feeds",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
    )


@scrape_bp.route("/scan-status", methods=["GET"])
@login_required
def scan_status():
    """The last/next-scan line on its own, so it can poll itself while a scan
    is actually in flight and then swap in the finished result.

    Lives in the scrape blueprint (scrape owns the data) but renders the
    dashboard partial, mirroring how `_sources_card.html` is shared.
    """
    from app.modules.scrape.service import scan_status_context

    return render_template("dashboard/_scan_status.html", **scan_status_context(current_user))


@scrape_bp.route("/profile/feeds", methods=["GET"])
@login_required
def feed_list():
    """The feed list on its own — filter chips and post-mutation swaps target
    this instead of re-rendering the whole AI tab."""
    return render_template(
        "settings/_feed_list.html", **_feed_list_ctx(request.args.get("filter", "all"))
    )


@scrape_bp.route("/profile/feeds/<int:feed_id>/remove", methods=["POST"])
@login_required
def submit_feed_remove(feed_id: int):
    from app.modules.scrape.service import remove_user_feed

    ok = remove_user_feed(current_user, feed_id)
    if not ok:
        abort(404)
    log_action("user.feed_removed", entity_type="user_feed", entity_id=str(feed_id))
    return render_template("settings/_feed_list.html", **_feed_list_ctx())


@scrape_bp.route("/profile/feeds/<int:feed_id>/toggle", methods=["POST"])
@login_required
def submit_feed_toggle(feed_id: int):
    from app.modules.scrape.service import toggle_user_feed

    new_value = toggle_user_feed(current_user, feed_id)
    if new_value is None:
        abort(404)
    log_action(
        "user.feed_toggled",
        entity_type="user_feed",
        entity_id=str(feed_id),
        changes={"active": new_value},
    )
    return render_template("settings/_feed_list.html", **_feed_list_ctx())


# ------------------------------------------------------------------ #
# Custom YouTube channel subscriptions — same AI/Kaynaklar profile tab
# ------------------------------------------------------------------ #


@scrape_bp.route("/profile/channels/add", methods=["POST"])
@login_required
def submit_channel_add():
    from app.modules.scrape.service import add_user_channel

    surface = request.form.get("surface")
    form = UserChannelForm()
    if form.validate_on_submit():
        channel, err = add_user_channel(current_user, form.url.data, form.label.data)
        if channel is not None:
            log_action(
                "user.channel_added",
                entity_type="user_channel",
                entity_id=str(channel.id),
                changes={"channel_id": channel.channel_id},
            )
            return _render_source_manager_result(
                surface,
                active_pane="channels",
                added=True,
                flash_msg=_("Channel added."),
                flash_kind="success",
            )
        return _render_source_manager_result(
            surface,
            active_pane="channels",
            flash_msg=_(err or "Could not add that channel."),
            flash_kind="danger",
        )
    return _render_source_manager_result(
        surface,
        active_pane="channels",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
    )


@scrape_bp.route("/profile/channels", methods=["GET"])
@login_required
def channel_list():
    """The channel list on its own — filter chips and post-mutation swaps
    target this instead of re-rendering the whole AI tab."""
    return render_template(
        "settings/_channel_list.html", **_channel_list_ctx(request.args.get("filter", "all"))
    )


@scrape_bp.route("/profile/channels/<int:channel_pk>/remove", methods=["POST"])
@login_required
def submit_channel_remove(channel_pk: int):
    from app.modules.scrape.service import remove_user_channel

    ok = remove_user_channel(current_user, channel_pk)
    if not ok:
        abort(404)
    log_action("user.channel_removed", entity_type="user_channel", entity_id=str(channel_pk))
    return render_template("settings/_channel_list.html", **_channel_list_ctx())


@scrape_bp.route("/profile/channels/<int:channel_pk>/toggle", methods=["POST"])
@login_required
def submit_channel_toggle(channel_pk: int):
    from app.modules.scrape.service import toggle_user_channel

    new_value = toggle_user_channel(current_user, channel_pk)
    if new_value is None:
        abort(404)
    log_action(
        "user.channel_toggled",
        entity_type="user_channel",
        entity_id=str(channel_pk),
        changes={"active": new_value},
    )
    return render_template("settings/_channel_list.html", **_channel_list_ctx())


# ------------------------------------------------------------------ #
# Custom Web Pages (non-RSS sites) — Faz 5.2
# ------------------------------------------------------------------ #


@scrape_bp.route("/profile/pages/add", methods=["POST"])
@login_required
def submit_page_add():
    from app.modules.scrape.service import add_user_page

    surface = request.form.get("surface")
    form = UserPageForm()
    if form.validate_on_submit():
        page, err = add_user_page(current_user, form.url.data, form.label.data, form.selector.data)
        if page is not None:
            log_action(
                "user.page_added",
                entity_type="user_page",
                entity_id=str(page.id),
                changes={"url": page.url, "mode": page.mode},
            )
            return _render_source_manager_result(
                surface,
                active_pane="pages",
                added=True,
                flash_msg=_("Web page added."),
                flash_kind="success",
            )
        return _render_source_manager_result(
            surface,
            active_pane="pages",
            flash_msg=_(err or "Could not add that page."),
            flash_kind="danger",
        )
    return _render_source_manager_result(
        surface,
        active_pane="pages",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
    )


@scrape_bp.route("/profile/pages", methods=["GET"])
@login_required
def page_list():
    """The page list on its own — post-mutation swaps target this."""
    return render_template(
        "settings/_page_list.html", **_page_list_ctx(request.args.get("filter", "all"))
    )


@scrape_bp.route("/profile/pages/<int:page_pk>/remove", methods=["POST"])
@login_required
def submit_page_remove(page_pk: int):
    from app.modules.scrape.service import remove_user_page

    ok = remove_user_page(current_user, page_pk)
    if not ok:
        abort(404)
    log_action("user.page_removed", entity_type="user_page", entity_id=str(page_pk))
    return render_template("settings/_page_list.html", **_page_list_ctx())


@scrape_bp.route("/profile/pages/<int:page_pk>/toggle", methods=["POST"])
@login_required
def submit_page_toggle(page_pk: int):
    from app.modules.scrape.service import toggle_user_page

    new_value = toggle_user_page(current_user, page_pk)
    if new_value is None:
        abort(404)
    log_action(
        "user.page_toggled",
        entity_type="user_page",
        entity_id=str(page_pk),
        changes={"active": new_value},
    )
    return render_template("settings/_page_list.html", **_page_list_ctx())


# ------------------------------------------------------------------ #
# Bluesky Social Accounts — Faz 5.3
# ------------------------------------------------------------------ #


@scrape_bp.route("/profile/bluesky/add", methods=["POST"])
@login_required
def submit_bluesky_add():
    from app.modules.scrape.service import add_user_bluesky

    surface = request.form.get("surface")
    form = UserBlueskyForm()
    if form.validate_on_submit():
        account, err = add_user_bluesky(current_user, form.handle.data)
        if account is not None:
            log_action(
                "user.bluesky_added",
                entity_type="user_bluesky",
                entity_id=str(account.id),
                changes={"handle": account.handle, "did": account.did},
            )
            return _render_source_manager_result(
                surface,
                active_pane="bluesky",
                added=True,
                flash_msg=_("Bluesky account followed."),
                flash_kind="success",
            )
        return _render_source_manager_result(
            surface,
            active_pane="bluesky",
            flash_msg=_(err or "Could not follow that Bluesky account."),
            flash_kind="danger",
        )
    return _render_source_manager_result(
        surface,
        active_pane="bluesky",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
    )


@scrape_bp.route("/profile/bluesky", methods=["GET"])
@login_required
def bluesky_list():
    """The bluesky list on its own — post-mutation swaps target this."""
    return render_template(
        "settings/_bluesky_list.html",
        **_bluesky_list_ctx(request.args.get("filter", "all")),
    )


@scrape_bp.route("/profile/bluesky/<int:bluesky_pk>/remove", methods=["POST"])
@login_required
def submit_bluesky_remove(bluesky_pk: int):
    from app.modules.scrape.service import remove_user_bluesky

    ok = remove_user_bluesky(current_user, bluesky_pk)
    if not ok:
        abort(404)
    log_action("user.bluesky_removed", entity_type="user_bluesky", entity_id=str(bluesky_pk))
    return render_template("settings/_bluesky_list.html", **_bluesky_list_ctx())


@scrape_bp.route("/profile/bluesky/<int:bluesky_pk>/toggle", methods=["POST"])
@login_required
def submit_bluesky_toggle(bluesky_pk: int):
    from app.modules.scrape.service import toggle_user_bluesky

    new_value = toggle_user_bluesky(current_user, bluesky_pk)
    if new_value is None:
        abort(404)
    log_action(
        "user.bluesky_toggled",
        entity_type="user_bluesky",
        entity_id=str(bluesky_pk),
        changes={"active": new_value},
    )
    return render_template("settings/_bluesky_list.html", **_bluesky_list_ctx())


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _widget_ctx() -> dict:
    """Everything `_scraper_widget.html` needs when rendered standalone.

    The widget used to read `active_sources` from a global context processor
    that returned the *deployment's* enabled sources — so a user who had muted
    arXiv still saw an "arXiv" badge. `scan_status_context` supplies that
    user's effective set, plus the last/next scan times.
    """
    from app.modules.academic.service import list_user_keywords
    from app.modules.scrape.service import scan_status_context

    return {
        "has_interests": bool(list_user_keywords(current_user)),
        **scan_status_context(current_user),
    }


def _render_card(link, *, flash_msg=None, flash_kind=None):
    """Render a single paper card — swap target for HTMX action handlers."""
    return render_template(
        "scrape/_paper_card.html",
        r=link,
        flash_msg=flash_msg,
        flash_kind=flash_kind,
    )


# ----------------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------------


@scrape_bp.route("/")
@login_required
def feed():
    from app.modules.academic.service import list_user_keywords
    from app.modules.scrape.service import scan_status_context, sources_card_context

    view = request.args.get("view", "discover")
    if view not in {"discover", "favorites", "dismissed", "all"}:
        view = "discover"
    q = request.args.get("q", "").strip()
    rows = list_user_papers(current_user, limit=100, view=view, q=q or None)
    counts = {
        "discover": count_user_papers(current_user, view="discover"),
        "favorites": count_user_papers(current_user, view="favorites"),
    }
    return render_template(
        "scrape/feed.html",
        rows=rows,
        view=view,
        counts=counts,
        q=q,
        user_keywords=list_user_keywords(current_user),
        # For the sidebar `_sources_card.html`, which shows the next-scan time
        # and the custom-feed counts. The scrape control itself lives in the
        # dashboard header, not here.
        **scan_status_context(current_user),
        **sources_card_context(current_user),
    )


@scrape_bp.route("/add-link", methods=["POST"])
@login_required
def add_link_route():
    """Manually add a web link to the user's library."""
    url = request.form.get("url", "").strip()
    if not url:
        if request.headers.get("HX-Request"):
            return (
                f'<div class="alert alert-warning py-2 mb-0 small">{_("Please enter a valid URL.")}</div>',
                400,
            )
        flash(_("Please enter a valid URL."), "warning")
        return redirect(url_for("scrape.feed"))

    try:
        from app.modules.scrape.service import add_paper_from_url

        link, created = add_paper_from_url(current_user, url)
        # Actor passed explicitly so core's audit middleware needs no
        # module-specific fallback (app/core never bends for app/modules).
        log_action(
            "add_paper_from_url",
            entity_type="paper",
            entity_id=link.paper_id,
            user_id=current_user.id,
        )
        msg = (
            _("Link added successfully.") if created else _("Link already exists in your library.")
        )
        if request.headers.get("HX-Request"):
            return render_template(
                "scrape/_paper_card.html",
                r=link,
                flash_msg=msg,
                flash_kind="success" if created else "info",
            )

        flash(msg, "success" if created else "info")
        return redirect(url_for("scrape.feed"))
    except ValueError as e:
        err_msg = str(e)
        if request.headers.get("HX-Request"):
            return f'<div class="alert alert-danger py-2 mb-0 small">{err_msg}</div>', 400
        flash(err_msg, "danger")
        return redirect(url_for("scrape.feed"))
    except Exception as e:
        logger.error("add_link_failed", user_id=current_user.id, url=url, error=str(e))
        err_msg = _("Failed to process URL. Please check the URL and try again.")
        if request.headers.get("HX-Request"):
            return f'<div class="alert alert-danger py-2 mb-0 small">{err_msg}</div>', 500
        flash(err_msg, "danger")
        return redirect(url_for("scrape.feed"))


def _get_internal_similar(link, limit=4):
    """Papers already in the user's own library that resemble this one — same
    matched keyword first, then same-category as a fallback. Cheap (DB only),
    so it's the always-available half of the Similar Papers panel."""
    from sqlalchemy import desc

    from app.modules.scrape.models import UserPaper

    q = UserPaper.query.filter(
        UserPaper.user_id == link.user_id, UserPaper.id != link.id, UserPaper.dismissed_at.is_(None)
    )
    if link.matched_keyword:
        q = q.filter(UserPaper.matched_keyword == link.matched_keyword)
    similar = q.order_by(desc(UserPaper.created_at)).limit(limit).all()
    if len(similar) < limit:
        cat_list = link.paper.categories or []
        if cat_list:
            fallback_q = UserPaper.query.filter(
                UserPaper.user_id == link.user_id,
                UserPaper.id != link.id,
                UserPaper.dismissed_at.is_(None),
            )
            if link.matched_keyword:
                fallback_q = fallback_q.filter(UserPaper.matched_keyword != link.matched_keyword)
            others = fallback_q.order_by(desc(UserPaper.created_at)).limit(20).all()
            for o in others:
                if len(similar) >= limit:
                    break
                o_cats = o.paper.categories or []
                if any(c in o_cats for c in cat_list):
                    similar.append(o)
    return similar[:limit]


def _get_external_similar(link, limit=5):
    """Recommendations from the wider literature via the Semantic Scholar
    Recommendations API. Network I/O + often rate-limited, so this is only
    ever called from the lazy-loaded /similar endpoint, never inline in the
    detail page. Returns [] when the paper has no DOI/S2 id or the API declines.
    """
    from app.modules.scrape.sources.semantic_scholar_source import fetch_similar_papers

    paper_id_or_doi = link.paper.doi or (
        link.paper.external_id if link.paper.source == "semantic_scholar" else None
    )
    if not paper_id_or_doi:
        return []
    try:
        return fetch_similar_papers(paper_id_or_doi, limit=limit)
    except Exception:  # noqa: BLE001
        return []


@scrape_bp.route("/<int:user_paper_id>")
@login_required
def detail(user_paper_id: int):
    """Paper detail with a 4-mode toggle (Original / TR / AI Analysis / RAG Chat).

    Mode comes from `?mode=original|tr|ai|chat`; default is "original". We pull
    the cached translation/analysis up front so the partial doesn't have
    to know about ai_service — anything missing just renders the "not yet"
    state plus a "Generate" button that hits the HTMX trigger endpoint.
    """
    from app.modules.scrape.ai_service import (
        get_analysis,
        get_translation,
        get_video_summary,
        is_ai_enabled,
    )

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    mark_seen(link)

    mode = request.args.get("mode", "original")
    if mode not in {"original", "tr", "ai", "chat"}:
        mode = "original"

    # RAG Chat messages
    chat_messages = []
    if mode == "chat":
        from app.modules.scrape.models import PaperChatMessage

        chat_messages = (
            PaperChatMessage.query.filter_by(user_paper_id=link.id)
            .order_by(PaperChatMessage.created_at.asc())
            .all()
        )

    template = "scrape/_detail_content.html" if _is_htmx() else "scrape/detail.html"
    return render_template(
        template,
        r=link,
        mode=mode,
        ai_enabled=is_ai_enabled(current_user),
        translation=get_translation(link.paper) if mode == "tr" else None,
        analysis=get_analysis(link.paper) if mode == "ai" else None,
        # Cache-only lookup — a GET never attempts generation, so there is no
        # "no transcript" signal here, only "cached" vs "not generated yet".
        # `no_transcript` (the route below sets it) is a transient result of
        # a POST attempt, not persisted state, so it's always False on load.
        summary=(
            get_video_summary(link.paper) if mode == "ai" and link.paper.kind == "video" else None
        ),
        no_transcript=False,
        chat_messages=chat_messages,
    )


@scrape_bp.route("/<int:user_paper_id>/similar", methods=["GET"])
@login_required
def similar(user_paper_id: int):
    """HTMX-loaded Similar Papers panel — internal (user's own library) plus
    external Semantic Scholar recommendations. Lazy so the S2 network call
    never blocks the detail page render."""
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    return render_template(
        "scrape/_similar_papers.html",
        r=link,
        internal=_get_internal_similar(link, limit=4),
        external=_get_external_similar(link, limit=5),
    )


@scrape_bp.route("/<int:user_paper_id>/chat", methods=["POST"])
@login_required
def send_chat_message(user_paper_id: int):
    """RAG Chat endpoint to ask Claude a question about the paper."""
    from app.extensions import db
    from app.modules.scrape.ai_service import ask_paper, is_ai_enabled
    from app.modules.scrape.models import PaperChatMessage

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    if not is_ai_enabled(current_user):
        abort(400, "AI not enabled")

    question = request.form.get("message", "").strip()
    if not question:
        return "", 204

    # 1. Save user question to DB
    user_msg = PaperChatMessage(user_paper_id=link.id, role="user", content=question)
    db.session.add(user_msg)
    db.session.commit()

    # 2. Get chat history for Claude context
    history_rows = (
        PaperChatMessage.query.filter_by(user_paper_id=link.id)
        .order_by(PaperChatMessage.created_at.asc())
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in history_rows[:-1]]

    # 3. Call the resolved LLM to get an answer
    answer = ask_paper(link.paper, question, history=history, user=current_user)
    if not answer:
        answer = _(
            "Claude did not return a response. Please check your credentials or try again later."
        )

    # 4. Save Claude response to DB
    assistant_msg = PaperChatMessage(user_paper_id=link.id, role="assistant", content=answer)
    db.session.add(assistant_msg)
    db.session.commit()

    log_action(
        "paper.chat_message_sent",
        entity_type="user_paper",
        entity_id=str(link.id),
        changes={"question_length": len(question), "answer_length": len(answer)},
    )

    # 5. Return both messages for HTMX to append to the chat log. Render via
    #    Jinja so the user's question and Claude's answer are auto-escaped —
    #    interpolating them into raw HTML here would be a stored-XSS hole
    #    (a question containing <script> would execute on every reload).
    return render_template(
        "scrape/_chat_message.html", role="user", content=user_msg.content
    ) + render_template(
        "scrape/_chat_message.html", role="assistant", content=assistant_msg.content
    )


@scrape_bp.route("/<int:user_paper_id>/analysis", methods=["POST"])
@login_required
def generate_analysis_route(user_paper_id: int):
    """HTMX: trigger Claude analysis. Swaps the AI mode panel with the
    rendered result. `?force=1` re-runs the cache."""
    from app.modules.scrape.ai_service import (
        get_or_generate_analysis,
        is_ai_enabled,
    )

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    if not is_ai_enabled(current_user):
        return render_template("scrape/_ai_disabled.html", kind="analysis")
    force = request.args.get("force") == "1"
    analysis = get_or_generate_analysis(link.paper, force=force, user=current_user)
    log_action(
        "paper.analysis_generated",
        entity_type="paper",
        entity_id=str(link.paper.id),
        changes={"force": force, "ok": analysis is not None},
    )
    return render_template("scrape/_ai_analysis.html", r=link, analysis=analysis)


@scrape_bp.route("/<int:user_paper_id>/video-summary", methods=["POST"])
@login_required
def generate_video_summary_route(user_paper_id: int):
    """HTMX: fetch this video's transcript (yt-dlp subprocess) and summarize
    it via the resolved LLM. Swaps the video-summary panel with the result.
    `?force=1` re-fetches the transcript and re-runs the cache.

    Cost/latency note: unlike generate_analysis_route (one LLM call),
    fetch_transcript shells out to yt-dlp (`_YTDLP_TIMEOUT = 30`s in
    youtube_channel_source.py) *before* the LLM call, both blocking this web
    worker synchronously — a request here can take ~10-30s longer than the
    plain analysis route. That's the same "no Celery hop for a single-paper,
    user-triggered action" trade-off the analysis route already accepts, just
    a slower instance of it; the template's hx-disabled-elt + hx-indicator
    are what keep a user from firing a second yt-dlp subprocess by
    double-clicking while the first is still running.
    """
    from app.modules.scrape.ai_service import (
        generate_video_summary,
        get_video_summary,
        is_ai_enabled,
    )
    from app.modules.scrape.sources.youtube_channel_source import fetch_transcript

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    if not is_ai_enabled(current_user):
        return render_template("scrape/_ai_disabled.html", kind="video_summary")

    force = request.args.get("force") == "1"
    if not force:
        cached = get_video_summary(link.paper)
        if cached is not None:
            # Cache hit — never shell out to yt-dlp just to throw the
            # transcript away.
            return render_template(
                "scrape/_video_summary.html",
                r=link,
                summary=cached,
                no_transcript=False,
                ai_enabled=True,
            )

    transcript = fetch_transcript(link.paper.external_id)
    summary = generate_video_summary(link.paper, transcript, user=current_user)
    # No captions vs. a generation failure both come back as None from
    # generate_video_summary — distinguish them so the template can tell the
    # user "this video has no captions" instead of a generic retry prompt.
    no_transcript = summary is None and not transcript
    log_action(
        "paper.video_summary_generated",
        entity_type="paper",
        entity_id=str(link.paper.id),
        changes={"force": force, "ok": summary is not None, "no_transcript": no_transcript},
    )
    return render_template(
        "scrape/_video_summary.html",
        r=link,
        summary=summary,
        no_transcript=no_transcript,
        ai_enabled=True,
    )


@scrape_bp.route("/<int:user_paper_id>/translate", methods=["POST"])
@login_required
def generate_translation_route(user_paper_id: int):
    """HTMX: trigger Claude translation. Swaps the TR mode panel with the
    rendered title+abstract translation."""
    from app.modules.scrape.ai_service import (
        get_or_generate_translation,
        is_ai_enabled,
    )

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    if not is_ai_enabled(current_user):
        return render_template("scrape/_ai_disabled.html", kind="translation")
    force = request.args.get("force") == "1"
    translation = get_or_generate_translation(link.paper, force=force, user=current_user)
    log_action(
        "paper.translation_generated",
        entity_type="paper",
        entity_id=str(link.paper.id),
        changes={"force": force, "ok": translation is not None},
    )
    return render_template("scrape/_ai_translation.html", r=link, translation=translation)


# ----------------------------------------------------------------------------
# Per-paper actions — HTMX swaps the card
# ----------------------------------------------------------------------------


@scrape_bp.route("/<int:user_paper_id>/cite", methods=["GET"])
@login_required
def cite_route(user_paper_id: int):
    """Plain-text BibTeX entry for the paper. Browser-side JS copies it to
    the clipboard; we also serve it as a download for the rare power user
    who wants the .bib file directly via ?download=1."""
    from flask import Response

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    bib = to_bibtex(link.paper)
    if request.args.get("download") == "1":
        filename = f"{link.paper.source}-{link.paper.external_id}.bib"
        return Response(
            bib,
            mimetype="application/x-bibtex",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return Response(bib, mimetype="text/plain; charset=utf-8")


@scrape_bp.route("/<int:user_paper_id>/open", methods=["POST"])
@login_required
def open_paper(user_paper_id: int):
    """Mark seen and redirect to source URL (non-HTMX, full nav)."""
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        flash(_("Paper not found."), "danger")
        return redirect(url_for("scrape.feed"))
    mark_seen(link)
    target = link.paper.url or url_for("scrape.feed")
    return redirect(target)


@scrape_bp.route("/<int:user_paper_id>/favorite/toggle", methods=["POST"])
@login_required
def toggle_favorite_route(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    is_now_fav = toggle_favorite(link)
    log_action(
        "paper.favorite_toggled",
        entity_type="user_paper",
        entity_id=str(link.id),
        changes={"is_favorite": is_now_fav},
    )
    if _is_htmx():
        # Detail-page button uses ?as=button to swap just itself; feed cards
        # swap the whole card so the badge ("Favorite"/"New") refreshes too.
        if request.args.get("as") == "button":
            return render_template("scrape/_favorite_button.html", r=link)
        return _render_card(link)
    flash(_("Saved to favorites.") if is_now_fav else _("Removed from favorites."), "success")
    return redirect(request.referrer or url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/dismiss", methods=["POST"])
@login_required
def dismiss(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    set_dismissed(link, True)
    log_action("paper.dismissed", entity_type="user_paper", entity_id=str(link.id))
    if _is_htmx():
        # Render an undo banner that replaces the card in-place; clicking
        # it un-dismisses without losing the slot.
        return render_template("scrape/_dismissed_undo.html", r=link)
    flash(_("Hidden from your feed."), "info")
    return redirect(url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/undismiss", methods=["POST"])
@login_required
def undismiss(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    set_dismissed(link, False)
    log_action("paper.undismissed", entity_type="user_paper", entity_id=str(link.id))
    if _is_htmx():
        return _render_card(link)
    return redirect(url_for("scrape.feed"))


# ----------------------------------------------------------------------------
# Notes — HTMX add/delete inside paper detail
# ----------------------------------------------------------------------------


@scrape_bp.route("/<int:user_paper_id>/notes", methods=["POST"])
@login_required
def add_note_route(user_paper_id: int):
    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    body = request.form.get("body", "")
    tag = request.form.get("tag", "")
    note = add_note(link, body, tag=tag)
    if note is None:
        # Empty body — re-render the list unchanged with a small inline message
        if _is_htmx():
            return render_template(
                "scrape/_notes_list.html",
                r=link,
                flash_msg=_("Empty notes are not saved."),
                flash_kind="danger",
            )
        flash(_("Empty notes are not saved."), "danger")
        return redirect(url_for("scrape.detail", user_paper_id=link.id))
    log_action(
        "paper.note_added",
        entity_type="paper_note",
        entity_id=str(note.id),
        changes={"tag": note.tag},
    )
    if _is_htmx():
        return render_template("scrape/_notes_list.html", r=link)
    return redirect(url_for("scrape.detail", user_paper_id=link.id))


@scrape_bp.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note_route(note_id: int):
    note = get_note_for_user(current_user, note_id)
    if note is None:
        abort(404)
    parent = note.user_paper
    delete_note(note)
    log_action("paper.note_deleted", entity_type="paper_note", entity_id=str(note_id))
    if _is_htmx():
        return render_template("scrape/_notes_list.html", r=parent)
    return redirect(url_for("scrape.detail", user_paper_id=parent.id))


@scrape_bp.route("/notes/<int:note_id>/view", methods=["GET"])
@login_required
def view_note_route(note_id: int):
    """Read-mode partial for a single note. Used by the inline edit form's
    Cancel button to swap back to the original card."""
    note = get_note_for_user(current_user, note_id)
    if note is None:
        abort(404)
    return render_template("scrape/_note_view.html", n=note)


@scrape_bp.route("/notes/<int:note_id>/edit", methods=["GET", "POST"])
@login_required
def edit_note_route(note_id: int):
    """Inline edit. GET returns the textarea swap; POST persists and swaps
    back to the read view. Empty body is rejected the same way add_note
    does — validation in one place."""
    note = get_note_for_user(current_user, note_id)
    if note is None:
        abort(404)

    if request.method == "GET":
        return render_template("scrape/_note_edit.html", n=note)

    body = request.form.get("body", "")
    tag = request.form.get("tag", "")
    ok = edit_note(note, body, tag=tag)
    if not ok:
        return render_template(
            "scrape/_note_edit.html",
            n=note,
            flash_msg=_("Empty notes are not saved."),
            flash_kind="danger",
        )
    log_action(
        "paper.note_edited",
        entity_type="paper_note",
        entity_id=str(note.id),
        changes={"tag": note.tag},
    )
    return render_template("scrape/_note_view.html", n=note)


# ----------------------------------------------------------------------------
# Followed authors (Faz 5.4)
# ----------------------------------------------------------------------------


def _render_authors_tab(**flash_kwargs):
    """Re-render the whole tab. Author lists are short (capped at
    MAX_USER_AUTHORS) so there is nothing to gain from a finer-grained swap,
    and one target means add/remove/toggle can't leave the count stale."""
    return _render_settings_tab("authors", **_authors_ctx(), **flash_kwargs)


@scrape_bp.route("/profile/authors/follow", methods=["POST"])
@login_required
def submit_author_follow():
    from app.modules.scrape.service import follow_author

    form = FollowAuthorForm()
    if not form.validate_on_submit():
        return _render_authors_tab(
            flash_kind="danger", flash_msg=_("Please enter an ORCID or OpenAlex author id.")
        )

    row, error = follow_author(current_user, form.identifier.data)
    if row is None:
        return _render_authors_tab(flash_kind="danger", flash_msg=_(error))

    log_action("user.author_followed", entity_type="user_author", entity_id=str(row.id))
    return _render_authors_tab(
        flash_kind="success",
        flash_msg=_("Now following %(name)s.", name=row.author_name),
    )


@scrape_bp.route("/profile/authors/<int:author_id>/pause", methods=["POST"])
@login_required
def submit_author_pause(author_id: int):
    from app.modules.scrape.service import toggle_user_author

    row = toggle_user_author(current_user, author_id)
    if row is None:
        abort(404)
    log_action("user.author_toggled", entity_type="user_author", entity_id=str(author_id))
    return _render_authors_tab()


@scrape_bp.route("/profile/authors/<int:author_id>/delete", methods=["POST"])
@login_required
def submit_author_delete(author_id: int):
    from app.modules.scrape.service import unfollow_author

    if not unfollow_author(current_user, author_id):
        abort(404)
    log_action("user.author_unfollowed", entity_type="user_author", entity_id=str(author_id))
    return _render_authors_tab(flash_kind="info", flash_msg=_("Author unfollowed."))


# ----------------------------------------------------------------------------
# Prior-art search (Faz 5.2)
# ----------------------------------------------------------------------------

#: How many words of the idea become search terms. The patent APIs match on
#: title/abstract text, so a whole paragraph would over-constrain EPO's CQL
#: (`ti,ab any` per term, OR-ed) into noise.
_PRIOR_ART_MAX_TERMS = 6

#: Turkish and English function words carry no signal for a patent search and
#: would each become their own OR clause. Deliberately a small hand-list, not
#: an NLP dependency — the idea text is short and the failure mode of a missed
#: stopword is one wasted clause.
_PRIOR_ART_STOPWORDS = {
    "bir", "ve", "veya", "ile", "için", "bu", "şu", "olan", "olarak", "daha",
    "gibi", "kadar", "her", "the", "and", "or", "with", "for", "this", "that",
    "a", "an", "of", "to", "in", "on", "is", "are", "be", "as", "by", "from",
}  # fmt: skip


def _prior_art_terms(idea: str) -> list[str]:
    """Search terms derived from a free-text invention description.

    The form asks for the idea once because the LLM assessment needs the prose
    while the adapters need keywords; this is the bridge. Longest words first:
    in a sentence like "a system for detecting bearing wear with vibration
    sensors", the long words are the technical ones.
    """
    import re

    words = re.findall(r"[\w-]{3,}", (idea or "").lower(), flags=re.UNICODE)
    seen: set[str] = set()
    terms: list[str] = []
    for word in sorted(words, key=len, reverse=True):
        if word in _PRIOR_ART_STOPWORDS or word in seen:
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) >= _PRIOR_ART_MAX_TERMS:
            break
    return terms


@scrape_bp.route("/patents", methods=["GET", "POST"])
@login_required
def prior_art():
    """Live prior-art search over the patent sources, plus an optional LLM
    novelty assessment.

    **Nothing here is persisted.** `service.search_patents_live` returns
    payloads rather than Paper rows: someone checking twenty wordings of an
    invention would otherwise leave hundreds of rows in `papers` that no
    user's feed ever wanted, and EPO's fair-use terms are happier with a
    search that keeps nothing. Only the nightly keyword scan persists.

    The assessment is optional and its absence is not an error — the patent
    list is the part with evidentiary value; the LLM output is a reading aid
    over it.
    """
    from app.modules.scrape.ai_service import analyze_novelty, is_ai_enabled
    from app.modules.scrape.forms import PriorArtForm
    from app.modules.scrape.service import patent_sources, search_patents_live

    form = PriorArtForm()
    sources = patent_sources()
    ctx = {
        "form": form,
        "sources": sources,
        "ai_available": is_ai_enabled(current_user),
        "results": None,
        "per_source": {},
        "assessment": None,
        "terms": [],
    }

    if not sources:
        # Keys missing, or the admin has not switched patents on. Say which,
        # rather than rendering an empty search box that silently finds
        # nothing — see the gates in docs/SCRAPING.md §5.
        return render_template("scrape/patents.html", **ctx)

    if form.validate_on_submit():
        idea = form.idea.data.strip()
        terms = _prior_art_terms(idea)
        results, per_source = search_patents_live(terms)
        ctx.update(results=results, per_source=per_source, terms=terms)

        if form.assess.data and results:
            # A missing assessment must not cost the user their results, which
            # are already in hand and are the useful half.
            try:
                ctx["assessment"] = analyze_novelty(idea, results, user=current_user)
            except Exception:  # noqa: BLE001
                logger.exception("prior_art_assessment_failed", user_id=current_user.id)

        log_action("patents.prior_art_search", entity_type="patent_search", entity_id=None)

    return render_template("scrape/patents.html", **ctx)


# ----------------------------------------------------------------------------
# Manual scrape
# ----------------------------------------------------------------------------


@scrape_bp.route("/run", methods=["POST"])
@login_required
def run_now():
    from app.modules.scrape.service import acquire_scrape_lock
    from app.tasks.scrape_tasks import run_for_user

    # Collapse duplicate presses: if a scrape is already queued/running for this
    # user, don't stack another task — just tell them it's already in progress.
    if not acquire_scrape_lock(current_user.id):
        msg = _("A scrape is already running for you — you'll be notified when it finishes.")
        if _is_htmx():
            return render_template(
                "scrape/_scraper_widget.html",
                already_running=True,
                **_widget_ctx(),
            )
        flash(msg, "info")
        return redirect(url_for("scrape.feed"))

    # lock_held: the lock was just claimed above, so the task must not try to
    # claim it again (and must still release it when it finishes).
    async_result = run_for_user.delay(current_user.id, trigger="manual", lock_held=True)

    # RSS is a separate pipeline (global ingest + per-user relevance linking),
    # so pressing "Scrape now" used to refresh the academic sources only and
    # silently leave the news feeds at last night's state — "I just scanned,
    # there's nothing new" was wrong rather than empty. Queue the feed task
    # too. It takes its own "feeds" lock, so an already-running feed pass makes
    # this a no-op instead of a double fetch + double LLM spend. Failure to
    # queue it must not cost the user their scrape, which is already away.
    feed_task_id = None
    try:
        from app.tasks.feed_tasks import link_for_user

        feed_task_id = getattr(link_for_user.delay(current_user.id), "id", None)
    except Exception:  # noqa: BLE001
        logger.exception("manual_feed_refresh_enqueue_failed", user_id=current_user.id)

    # Subscribed YouTube channels are a third pipeline, and they had exactly the
    # bug described above for RSS: "Scrape now" left them at last night's state,
    # so a user who had just subscribed to a channel pressed scrape, saw nothing,
    # and had no way to tell whether it had been checked at all. Same contract as
    # the feed task — its own "channels" lock makes a concurrent run a no-op, and
    # a failure to queue must not cost the user the scrape that is already away.
    channel_task_id = None
    try:
        from app.tasks.channel_tasks import ingest_for_user as ingest_channels_for_user

        channel_task_id = getattr(
            ingest_channels_for_user.delay(current_user.id, trigger="manual"), "id", None
        )
    except Exception:  # noqa: BLE001
        logger.exception("manual_channel_refresh_enqueue_failed", user_id=current_user.id)

    log_action(
        "scrape.manual_run",
        entity_type="user",
        entity_id=str(current_user.id),
        changes={
            "task_id": getattr(async_result, "id", None),
            "feed_task_id": feed_task_id,
            "channel_task_id": channel_task_id,
        },
    )
    if _is_htmx():
        return render_template(
            "scrape/_scrape_spinner.html",
            task_id=async_result.id,
            since=int(time.time()),
            phase="queued",
        )
    flash(_("Scrape queued — papers will appear here once the worker finishes."), "info")
    return redirect(url_for("scrape.feed"))


#: How long a manually-triggered scrape may sit in the queue before we stop
#: spinning and say so. Anything longer means nothing is consuming the queue —
#: almost always "the Celery worker isn't running" — and an endless spinner is
#: the least useful way to communicate that.
_QUEUE_WAIT_LIMIT = 90


def _celery_task_finished(task_id: str) -> bool | None:
    """Has this Celery task finished? None when we can't tell.

    `AsyncResult(task_id)` without an explicit app resolves through
    `celery.current_app`, which in a web process can be a bare default Celery
    app whose backend is `DisabledBackend` — calling `.ready()` on that raises
    AttributeError and 500s the poll. Bind our configured app explicitly, and
    treat any backend failure as "unknown" so the DB fallback decides.
    """
    from celery.result import AsyncResult

    from app.tasks import celery_app

    try:
        return bool(AsyncResult(task_id, app=celery_app).ready())
    except Exception:  # noqa: BLE001 — no/unreachable result backend
        logger.warning("scrape_status_backend_unavailable", task_id=task_id)
        return None


@scrape_bp.route("/status/<task_id>", methods=["GET"])
@login_required
def scrape_status_poll(task_id: str):
    """Drive the manual-scrape spinner.

    Celery's result backend is treated as a hint, not the source of truth: the
    ScanRun rows say what actually happened, and they exist even when no result
    backend is configured. That also lets us distinguish "queued" from
    "scanning", and give up honestly when nothing ever picks the task up.
    """
    from flask import Response

    from app.modules.scrape.service import (
        aware,
        last_scan_run,
        open_scan_run,
        reap_stale_runs,
    )

    since = request.args.get("since", type=int) or int(time.time())
    waited = max(0, int(time.time()) - since)

    reap_stale_runs(current_user)
    active = open_scan_run(current_user, "scrape")
    finished = last_scan_run(current_user, "scrape")

    # 5s of slack: the row is written a moment after we stamped `since`, and
    # clock resolution between the two shouldn't decide whether we say "done".
    started_since_request = (
        finished is not None and aware(finished.started_at).timestamp() >= since - 5
    )
    done = _celery_task_finished(task_id) is True or started_since_request

    if done:
        response = Response(render_template("scrape/_scraper_widget.html", **_widget_ctx()))
        response.headers["HX-Refresh"] = "true"
        return response

    if active is None and waited > _QUEUE_WAIT_LIMIT:
        # Nothing claimed the task within the window: stop spinning and say so
        # rather than implying work is happening.
        return render_template(
            "scrape/_scraper_widget.html",
            worker_stalled=True,
            **_widget_ctx(),
        )

    return render_template(
        "scrape/_scrape_spinner.html",
        task_id=task_id,
        since=since,
        phase="running" if active is not None else "queued",
    )


@scrape_bp.route("/bulk-action", methods=["POST"])
@login_required
def bulk_action():
    from datetime import UTC, datetime

    from app.extensions import db
    from app.modules.scrape.models import UserPaper

    paper_ids = request.form.getlist("paper_ids")
    action = request.form.get("action")

    if not paper_ids or not action:
        flash(_("No papers selected or action specified."), "warning")
        return redirect(url_for("scrape.feed"))

    try:
        paper_ids = [int(pid) for pid in paper_ids]
    except ValueError:
        flash(_("Invalid paper selection."), "danger")
        return redirect(url_for("scrape.feed"))

    links = UserPaper.query.filter(
        UserPaper.id.in_(paper_ids), UserPaper.user_id == current_user.id
    ).all()

    if not links:
        flash(_("No matching papers found."), "warning")
        return redirect(url_for("scrape.feed"))

    if action == "favorite":
        for link in links:
            link.is_favorite = True
        db.session.commit()
        flash(_("Selected papers added to favorites."), "success")
    elif action == "read_later":
        for link in links:
            link.read_later = True
        db.session.commit()
        flash(_("Selected papers marked as Read Later."), "success")
    elif action == "dismiss":
        for link in links:
            link.dismissed_at = datetime.now(UTC)
        db.session.commit()
        flash(_("Selected papers hidden."), "info")
    else:
        flash(_("Unknown action."), "danger")

    log_action(
        "paper.bulk_action",
        entity_type="user_paper",
        entity_id="bulk",
        changes={"count": len(links), "action": action},
    )

    return redirect(request.referrer or url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/read-later/toggle", methods=["POST"])
@login_required
def toggle_read_later_route(user_paper_id: int):
    from app.modules.scrape.service import toggle_read_later

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)
    is_now_rl = toggle_read_later(link)
    log_action(
        "paper.read_later_toggled",
        entity_type="user_paper",
        entity_id=str(link.id),
        changes={"read_later": is_now_rl},
    )
    if _is_htmx():
        if request.args.get("as") == "button":
            return render_template("scrape/_read_later_button.html", r=link)
        return _render_card(link)
    flash(_("Added to Read Later.") if is_now_rl else _("Removed from Read Later."), "success")
    return redirect(request.referrer or url_for("scrape.feed"))


@scrape_bp.route("/<int:user_paper_id>/export-notes", methods=["GET"])
@login_required
def export_notes_route(user_paper_id: int):
    """Export all notes for a specific user paper as Markdown file download."""
    from datetime import datetime

    from flask import Response

    link = get_user_paper(current_user, user_paper_id)
    if link is None:
        abort(404)

    title = link.paper.title or "Untitled"
    external_id = link.paper.external_id or ""

    md_content = f"# Notes on: {title}\n"
    md_content += f"- Source: {link.paper.source} ({external_id})\n"
    md_content += f"- Exported on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    md_content += "## Notes\n\n"

    if not link.notes:
        md_content += "_No notes taken for this paper._\n"
    else:
        for n in link.notes:
            tag_header = f"### [{n.tag.upper()}]" if n.tag else "### [Note]"
            md_content += f"{tag_header} ({n.created_at.strftime('%Y-%m-%d %H:%M')})\n"
            md_content += f"{n.body}\n\n"

    filename = f"notes-{link.paper.source}-{external_id}.md".replace("/", "-")
    return Response(
        md_content,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Side-effect: registers the "AI Settings" profile tab and the deployment-level
# source toggles. Runs when this module is imported (app/__init__.py imports
# scrape_bp from here at startup).
_register_tabs()
_register_system_toggles()
