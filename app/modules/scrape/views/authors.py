"""Author following, author search and author-group handlers."""

from __future__ import annotations

import structlog
from flask import abort, flash, redirect, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape import routes
from app.modules.scrape.forms import (
    AuthorGroupForm,
    AuthorSearchForm,
    FollowAuthorForm,
)
from app.modules.scrape.routes import scrape_bp

logger = structlog.get_logger()


# ----------------------------------------------------------------------------
# Followed authors (Faz 5.4)
# ----------------------------------------------------------------------------


def _render_authors_tab(*, search_query: str | None = None, search_results=None, **flash_kwargs):
    """Re-render the whole tab. Author lists are short (capped at
    MAX_USER_AUTHORS) so there is nothing to gain from a finer-grained swap,
    and one target means add/remove/toggle can't leave the count stale. Group
    mutations follow the same one-target rule for the same reason.

    `search_query`/`search_results` are not part of `_authors_ctx()` — they
    are the transient result of a search POST, not standing tab state, so
    every other action (follow, pause, group CRUD, ...) re-renders the tab
    with both left at their `None` default (search box empty, no results
    shown) rather than needing to re-run or cache the last search.
    """
    return routes._render_settings_tab(
        "authors",
        **routes._authors_ctx(),
        search_query=search_query,
        search_results=search_results,
        **flash_kwargs,
    )


@scrape_bp.route("/profile/authors/follow", methods=["POST"])
@login_required
def submit_author_follow():
    from app.modules.scrape.service import follow_author

    form = FollowAuthorForm()
    if not form.validate_on_submit():
        return routes._render_authors_tab(
            flash_kind="danger", flash_msg=_("Please enter an ORCID or OpenAlex author id.")
        )

    row, error = follow_author(current_user, form.identifier.data)
    if row is None:
        return routes._render_authors_tab(flash_kind="danger", flash_msg=_(error))

    log_action("user.author_followed", entity_type="user_author", entity_id=str(row.id))
    return routes._render_authors_tab(
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
    return routes._render_authors_tab()


@scrape_bp.route("/profile/authors/<int:author_id>/delete", methods=["POST"])
@login_required
def submit_author_delete(author_id: int):
    from app.modules.scrape.service import unfollow_author

    if not unfollow_author(current_user, author_id):
        abort(404)
    log_action("user.author_unfollowed", entity_type="user_author", entity_id=str(author_id))
    return routes._render_authors_tab(flash_kind="info", flash_msg=_("Author unfollowed."))


# ----------------------------------------------------------------------------
# Author name search (Faz 6, ADR-0003) — candidates only, the human picks
# ----------------------------------------------------------------------------


@scrape_bp.route("/profile/authors/search", methods=["POST"])
@login_required
def submit_author_search():
    """Free-text OpenAlex author search. Never follows or groups anyone by
    itself — it only returns candidates for `submit_author_follow` (Follow)
    or `submit_author_add_to_group` (Add to group) to act on, each keyed by
    OpenAlex id, per ADR-0003."""
    from app.modules.scrape.ratelimit import SourceThrottledError
    from app.modules.scrape.sources import openalex_source as oa

    form = AuthorSearchForm()
    if not form.validate_on_submit():
        return routes._render_authors_tab(
            flash_kind="danger", flash_msg=_("Please correct the errors below.")
        )

    name = form.name.data.strip()
    try:
        results = oa.search_authors(name, limit=10)
    except SourceThrottledError:
        # A rate limit is not a 500: the rest of the tab (follows, groups)
        # still works, only the search itself must wait.
        return routes._render_authors_tab(
            search_query=name,
            flash_kind="warning",
            flash_msg=_("OpenAlex is busy right now — please try the search again in a moment."),
        )
    except Exception:  # noqa: BLE001 — a lookup failure is user-facing, not a 500
        logger.exception("author_search_failed", user_id=current_user.id)
        return routes._render_authors_tab(
            search_query=name,
            flash_kind="danger",
            flash_msg=_("Could not reach OpenAlex right now. Please try again."),
        )

    if not results:
        return routes._render_authors_tab(
            search_query=name,
            search_results=[],
            flash_kind="info",
            flash_msg=_("No authors found for that name."),
        )
    return routes._render_authors_tab(search_query=name, search_results=results)


@scrape_bp.route("/profile/authors/search/add-to-group", methods=["POST"])
@login_required
def submit_author_add_to_group():
    """Resolve a search candidate and add them to a group in one step.

    Critical behaviour (see CLAUDE.md / ADR-0003): the author is resolved
    with `activate=False` — group membership is "include this author in a
    report I ask for", never "push their new papers into my nightly feed".
    An already-followed author (active or paused) is unaffected either way;
    see `follow_author`'s docstring.
    """
    from app.modules.scrape.service import add_group_member, follow_author

    openalex_id = (request.form.get("openalex_id") or "").strip()
    try:
        group_id = int(request.form.get("group_id", ""))
    except (TypeError, ValueError):
        group_id = None

    if not openalex_id or group_id is None:
        return routes._render_authors_tab(
            flash_kind="danger", flash_msg=_("Please choose a group and a candidate.")
        )

    row, error = follow_author(current_user, openalex_id, activate=False)
    if row is None:
        return routes._render_authors_tab(flash_kind="danger", flash_msg=_(error))

    member, error = add_group_member(current_user, group_id, row.id)
    if member is None:
        return routes._render_authors_tab(flash_kind="danger", flash_msg=_(error))

    log_action(
        "user.author_group_member_added",
        entity_type="author_group_member",
        entity_id=str(member.id),
        changes={"group_id": group_id, "openalex_id": openalex_id, "via": "search"},
    )
    return routes._render_authors_tab(
        flash_kind="success",
        flash_msg=_("%(name)s added to the group.", name=row.author_name),
    )


# ----------------------------------------------------------------------------
# Author groups (Faz 6) — named sets of followed authors, for on-demand reports
# ----------------------------------------------------------------------------


def _owned_group_or_none(group_id: int):
    """Ownership-scoped group lookup for routes.

    Goes through `list_author_groups(user)` (already filters to
    `current_user`) rather than a bare `AuthorGroup.query.get(group_id)` —
    same "route never filters by user_id itself" rule the rest of this module
    follows for `UserAuthor` rows. A group id belonging to another user is
    indistinguishable here from an unknown one, and both are a 404.
    """
    from app.modules.scrape.service import list_author_groups

    for g in list_author_groups(current_user):
        if g.id == group_id:
            return g
    return None


@scrape_bp.route("/profile/authors/groups/create", methods=["POST"])
@login_required
def submit_group_create():
    from app.modules.scrape.service import create_author_group

    form = AuthorGroupForm()
    if not form.validate_on_submit():
        return routes._render_authors_tab(
            flash_kind="danger", flash_msg=_("Please correct the errors below.")
        )

    group, error = create_author_group(current_user, form.name.data, form.description.data)
    if group is None:
        return routes._render_authors_tab(flash_kind="danger", flash_msg=_(error))

    log_action("user.author_group_created", entity_type="author_group", entity_id=str(group.id))
    return routes._render_authors_tab(flash_kind="success", flash_msg=_("Group created."))


@scrape_bp.route("/profile/authors/groups/<int:group_id>/rename", methods=["POST"])
@login_required
def submit_group_rename(group_id: int):
    from app.modules.scrape.service import rename_author_group

    if routes._owned_group_or_none(group_id) is None:
        abort(404)

    name = request.form.get("name", "")
    group, error = rename_author_group(current_user, group_id, name)
    if group is None:
        return routes._render_authors_tab(flash_kind="danger", flash_msg=_(error))

    log_action(
        "user.author_group_renamed",
        entity_type="author_group",
        entity_id=str(group_id),
        changes={"name": group.name},
    )
    return routes._render_authors_tab(flash_kind="success", flash_msg=_("Group renamed."))


@scrape_bp.route("/profile/authors/groups/<int:group_id>/delete", methods=["POST"])
@login_required
def submit_group_delete(group_id: int):
    from app.modules.scrape.service import delete_author_group

    if routes._owned_group_or_none(group_id) is None:
        abort(404)

    delete_author_group(current_user, group_id)
    log_action("user.author_group_deleted", entity_type="author_group", entity_id=str(group_id))
    return routes._render_authors_tab(flash_kind="info", flash_msg=_("Group deleted."))


@scrape_bp.route("/profile/authors/groups/<int:group_id>/members/add", methods=["POST"])
@login_required
def submit_group_member_add(group_id: int):
    """Add an already-followed author to a group — the other of the two ways
    to add a member (the search-based one is `submit_author_add_to_group`)."""
    from app.modules.scrape.service import add_group_member

    if routes._owned_group_or_none(group_id) is None:
        abort(404)

    try:
        user_author_id = int(request.form.get("user_author_id", ""))
    except (TypeError, ValueError):
        return routes._render_authors_tab(
            flash_kind="danger", flash_msg=_("Please choose an author.")
        )

    member, error = add_group_member(current_user, group_id, user_author_id)
    if member is None:
        return routes._render_authors_tab(flash_kind="danger", flash_msg=_(error))

    log_action(
        "user.author_group_member_added",
        entity_type="author_group_member",
        entity_id=str(member.id),
        changes={"group_id": group_id, "via": "existing"},
    )
    return routes._render_authors_tab(
        flash_kind="success", flash_msg=_("Author added to the group.")
    )


@scrape_bp.route(
    "/profile/authors/groups/<int:group_id>/members/<int:member_id>/remove", methods=["POST"]
)
@login_required
def submit_group_member_remove(group_id: int, member_id: int):
    """`member_id` is the `AuthorGroupMember.id`, not the `UserAuthor.id` —
    see `remove_group_member`'s docstring and `_authors_ctx`'s
    `group_membership_ids`, which is what the template builds the URL from."""
    from app.modules.scrape.service import remove_group_member

    if routes._owned_group_or_none(group_id) is None:
        abort(404)
    if not remove_group_member(current_user, group_id, member_id):
        abort(404)

    log_action(
        "user.author_group_member_removed",
        entity_type="author_group_member",
        entity_id=str(member_id),
        changes={"group_id": group_id},
    )
    return routes._render_authors_tab(
        flash_kind="info", flash_msg=_("Author removed from the group.")
    )


@scrape_bp.route("/profile/authors/groups/<int:group_id>/report", methods=["POST"])
@login_required
def submit_group_report(group_id: int):
    """Queue an "author_group" report for this group (Faz 6).

    Mirrors `report_create`'s HTMX/plain-nav split exactly — 202 + HX-Redirect
    when this came from an HTMX form (it does, from inside the authors tab),
    a plain redirect otherwise. `create_report` re-validates ownership and
    non-empty membership itself, so `_owned_group_or_none` here is only for
    the 404-vs-flash distinction, same as every other group route.
    """
    from app.modules.scrape.report_service import create_report
    from app.tasks.report_tasks import generate

    if routes._owned_group_or_none(group_id) is None:
        abort(404)

    try:
        years = int(request.form.get("years", ""))
    except (TypeError, ValueError):
        years = None

    report, error = create_report(
        current_user, "author_group", {"group_id": group_id, "years": years}
    )
    if report is None:
        return routes._render_authors_tab(flash_kind="danger", flash_msg=_(error))

    generate.delay(report.id)
    log_action(
        "report.created",
        entity_type="report",
        entity_id=str(report.id),
        changes={"kind": "author_group", "group_id": group_id, "years": years},
    )

    if routes._is_htmx():
        from flask import Response

        return Response(
            "",
            status=202,
            headers={"HX-Redirect": url_for("scrape.report_detail", report_id=report.id)},
        )
    flash(_("Report queued — it will appear here once generated."), "success")
    return redirect(url_for("scrape.report_detail", report_id=report.id))
