"""Custom feed / channel / page / Bluesky subscription handlers."""

from __future__ import annotations

import structlog
from flask import abort, render_template, request
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape import routes
from app.modules.scrape.forms import (
    UserBlueskyForm,
    UserChannelForm,
    UserFeedForm,
    UserPageForm,
)
from app.modules.scrape.routes import scrape_bp

logger = structlog.get_logger()


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
            return routes._render_source_manager_result(
                surface,
                active_pane="feeds",
                added=True,
                flash_msg=_("Feed added."),
                flash_kind="success",
            )
        return routes._render_source_manager_result(
            surface,
            active_pane="feeds",
            flash_msg=_(err or "Could not add that feed."),
            flash_kind="danger",
        )
    return routes._render_source_manager_result(
        surface,
        active_pane="feeds",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
    )


@scrape_bp.route("/profile/feeds", methods=["GET"])
@login_required
def feed_list():
    """The feed list on its own — filter chips and post-mutation swaps target
    this instead of re-rendering the whole AI tab."""
    return render_template(
        "settings/_feed_list.html", **routes._feed_list_ctx(request.args.get("filter", "all"))
    )


@scrape_bp.route("/profile/feeds/<int:feed_id>/remove", methods=["POST"])
@login_required
def submit_feed_remove(feed_id: int):
    from app.modules.scrape.service import remove_user_feed

    ok = remove_user_feed(current_user, feed_id)
    if not ok:
        abort(404)
    log_action("user.feed_removed", entity_type="user_feed", entity_id=str(feed_id))
    return render_template("settings/_feed_list.html", **routes._feed_list_ctx())


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
    return render_template("settings/_feed_list.html", **routes._feed_list_ctx())


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
            return routes._render_source_manager_result(
                surface,
                active_pane="channels",
                added=True,
                flash_msg=_("Channel added."),
                flash_kind="success",
            )
        return routes._render_source_manager_result(
            surface,
            active_pane="channels",
            flash_msg=_(err or "Could not add that channel."),
            flash_kind="danger",
        )
    return routes._render_source_manager_result(
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
        "settings/_channel_list.html", **routes._channel_list_ctx(request.args.get("filter", "all"))
    )


@scrape_bp.route("/profile/channels/<int:channel_pk>/remove", methods=["POST"])
@login_required
def submit_channel_remove(channel_pk: int):
    from app.modules.scrape.service import remove_user_channel

    ok = remove_user_channel(current_user, channel_pk)
    if not ok:
        abort(404)
    log_action("user.channel_removed", entity_type="user_channel", entity_id=str(channel_pk))
    return render_template("settings/_channel_list.html", **routes._channel_list_ctx())


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
    return render_template("settings/_channel_list.html", **routes._channel_list_ctx())


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
            return routes._render_source_manager_result(
                surface,
                active_pane="pages",
                added=True,
                flash_msg=_("Web page added."),
                flash_kind="success",
            )
        return routes._render_source_manager_result(
            surface,
            active_pane="pages",
            flash_msg=_(err or "Could not add that page."),
            flash_kind="danger",
        )
    return routes._render_source_manager_result(
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
        "settings/_page_list.html", **routes._page_list_ctx(request.args.get("filter", "all"))
    )


@scrape_bp.route("/profile/pages/<int:page_pk>/remove", methods=["POST"])
@login_required
def submit_page_remove(page_pk: int):
    from app.modules.scrape.service import remove_user_page

    ok = remove_user_page(current_user, page_pk)
    if not ok:
        abort(404)
    log_action("user.page_removed", entity_type="user_page", entity_id=str(page_pk))
    return render_template("settings/_page_list.html", **routes._page_list_ctx())


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
    return render_template("settings/_page_list.html", **routes._page_list_ctx())


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
            return routes._render_source_manager_result(
                surface,
                active_pane="bluesky",
                added=True,
                flash_msg=_("Bluesky account followed."),
                flash_kind="success",
            )
        return routes._render_source_manager_result(
            surface,
            active_pane="bluesky",
            flash_msg=_(err or "Could not follow that Bluesky account."),
            flash_kind="danger",
        )
    return routes._render_source_manager_result(
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
        **routes._bluesky_list_ctx(request.args.get("filter", "all")),
    )


@scrape_bp.route("/profile/bluesky/<int:bluesky_pk>/remove", methods=["POST"])
@login_required
def submit_bluesky_remove(bluesky_pk: int):
    from app.modules.scrape.service import remove_user_bluesky

    ok = remove_user_bluesky(current_user, bluesky_pk)
    if not ok:
        abort(404)
    log_action("user.bluesky_removed", entity_type="user_bluesky", entity_id=str(bluesky_pk))
    return render_template("settings/_bluesky_list.html", **routes._bluesky_list_ctx())


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
    return render_template("settings/_bluesky_list.html", **routes._bluesky_list_ctx())
