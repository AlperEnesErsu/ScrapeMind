"""Research-settings tab contexts and the AI / source-manager handlers."""

from __future__ import annotations

import structlog
from flask import render_template
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape import routes
from app.modules.scrape.forms import (
    AiSettingsForm,
    AuthorGroupForm,
    AuthorSearchForm,
    FollowAuthorForm,
    UserBlueskyForm,
    UserChannelForm,
    UserFeedForm,
    UserPageForm,
)
from app.modules.scrape.routes import scrape_bp

logger = structlog.get_logger()


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
        **routes._feed_list_ctx(),
        **routes._channel_list_ctx(),
        **routes._page_list_ctx(),
        **routes._bluesky_list_ctx(),
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
        **routes._source_manager_ctx(clear_forms=clear_forms),
    }


def _authors_ctx():
    """Context for the "Followed Authors" profile tab (Faz 5.4 + Faz 6).

    Surfaces the user's own ORCID from the Identifiers tab so following your
    own publications is one click rather than a copy-paste between two
    screens. Reading it here (rather than putting this UI inside the academic
    module) keeps the data's owner and its UI in the same module — scrape owns
    `UserAuthor`, and scrape already imports `academic.service` for keywords.

    Faz 6 adds author groups (named sets of followed authors, gathered for an
    on-demand "author_group" report — see `service.py`'s `AuthorGroup`
    section and `docs/adr/0003-yazar-isim-aramasi.md`). `group_members` reuses
    `list_group_members(group)` verbatim (per-group `UserAuthor` rows, for
    display); `group_membership_ids` is a *separate* lookup built from
    `AuthorGroup.members` (the raw `AuthorGroupMember` rows) because
    `remove_group_member` needs the membership row's id, not the author's —
    `list_group_members`'s join collapses that id away. `group_available_members`
    is each group's followed-authors list minus whoever is already in it, for
    the "add an existing follow" picker.
    """
    from app.modules.academic.service import list_user_identifiers
    from app.modules.scrape.report_service import REPORT_MAX_YEARS, REPORT_MIN_YEARS
    from app.modules.scrape.service import (
        MAX_AUTHOR_GROUPS,
        MAX_GROUP_MEMBERS,
        MAX_USER_AUTHORS,
        list_author_groups,
        list_group_members,
        list_user_authors,
    )

    own = [i.value for i in list_user_identifiers(current_user, type_code="orcid")]
    authors = list_user_authors(current_user)
    followed_orcids = {a.orcid for a in authors if a.orcid}

    groups = list_author_groups(current_user)
    group_members = {g.id: list_group_members(g) for g in groups}
    group_membership_ids = {g.id: {m.user_author_id: m.id for m in g.members} for g in groups}
    group_available_members = {
        g.id: [a for a in authors if a.id not in group_membership_ids[g.id]] for g in groups
    }

    return {
        "form": FollowAuthorForm(),
        "search_form": AuthorSearchForm(),
        "group_form": AuthorGroupForm(),
        "authors": authors,
        "author_count": len(authors),
        "max_user_authors": MAX_USER_AUTHORS,
        # Only ORCIDs not already followed — offering "follow yourself" to
        # someone who already does is noise.
        "own_orcids": [o for o in own if o not in followed_orcids],
        "groups": groups,
        "group_count": len(groups),
        "max_author_groups": MAX_AUTHOR_GROUPS,
        "max_group_members": MAX_GROUP_MEMBERS,
        "group_members": group_members,
        "group_membership_ids": group_membership_ids,
        "group_available_members": group_available_members,
        # A small, fixed range for the per-group "generate a file" picker
        # rather than a bound WTForm SelectField — that field would be
        # rendered once per group from one shared form instance, producing
        # duplicate `id="years"` markup across group panels.
        "year_choices": list(range(REPORT_MIN_YEARS, REPORT_MAX_YEARS + 1)),
    }


def _register_tabs():
    """Register the scrape module's settings tabs -- called at startup, when
    this module is imported. They land on the workspace page, not the profile
    (issue #58); the academic module names that page and adds its menu entry.
    """
    from app.core.settings.tab_registry import register_profile_tab
    from app.modules.scrape.alert_routes import alerts_tab_ctx, zotero_tab_ctx

    register_profile_tab("ai", "bi-robot", "AI Settings", routes._ai_ctx)
    register_profile_tab("authors", "bi-person-badge", "Followed Authors", routes._authors_ctx)
    register_profile_tab("alerts", "bi-bell", "Saved Searches", alerts_tab_ctx)
    register_profile_tab("zotero", "bi-box-arrow-up-right", "Zotero", zotero_tab_ctx)


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
        credentials_ok=lambda: (
            bool(os.getenv("EPO_OPS_KEY") and os.getenv("EPO_OPS_SECRET"))
            or bool(os.getenv("PATENTSVIEW_API_KEY"))
        ),
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
        "settings/_source_manager.html", surface="modal", **routes._source_manager_ctx()
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
            **routes._source_manager_ctx(clear_forms=added),
            **flash_kwargs,
        )
    return routes._render_settings_tab(
        "ai", active_pane=active_pane, **flash_kwargs, **routes._ai_ctx(clear_forms=added)
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
            return routes._render_settings_tab(
                "ai",
                flash_msg=_("Nothing to save — enter a key or a model override."),
                flash_kind="warning",
                **routes._ai_ctx(),
            )
        set_user_llm_key(current_user, key or None, model or None)
        log_action(
            "user.llm_key_updated",
            entity_type="user",
            entity_id=current_user.id,
            changes={"key_updated": bool(key), "model": model or None},
        )
        return routes._render_settings_tab(
            "ai", flash_msg=_("AI settings saved."), flash_kind="success", **routes._ai_ctx()
        )
    return routes._render_settings_tab(
        "ai",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
        **routes._ai_ctx(),
    )


@scrape_bp.route("/profile/ai/clear", methods=["POST"])
@login_required
def submit_ai_clear():
    from app.modules.scrape.ai_service import clear_user_llm_key

    clear_user_llm_key(current_user)
    log_action("user.llm_key_cleared", entity_type="user", entity_id=current_user.id)
    return routes._render_settings_tab(
        "ai", flash_msg=_("Your API key was removed."), flash_kind="success", **routes._ai_ctx()
    )
