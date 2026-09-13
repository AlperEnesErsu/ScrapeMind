from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from flask import current_app, request, url_for
from werkzeug.routing import BuildError

from app.core.models.menu import MenuItem
from app.core.models.user import User
from app.core.rbac.service import get_user_permissions

logger = structlog.get_logger()


@dataclass
class MenuNode:
    item: MenuItem
    children: list[MenuNode] = field(default_factory=list)

    @property
    def href(self) -> str:
        """The link to render, resolved at render time.

        Never raises on bad *data* -- which is the whole point -- but it will
        raise outside a request, because building a URL needs one. That is
        correct: a caller reading `href` with no request is making a mistake,
        and hiding it here would trade a clear error for a silent "#".

        Deliberately a property rather than a field computed in the builder.
        Building a URL needs a request context, and `build_menu_for_user` did
        not require one before -- tightening that contract broke a test that
        builds a menu outside a request, which is a fair thing to do. So
        reachability is decided at build time (which needs only an app
        context) and the URL is built here, where a request exists.

        `_drop_unreachable` has already removed endpoints this app does not
        have. The guard below catches the remaining case: an endpoint that
        exists but needs URL parameters a menu row cannot supply. Falling back
        beats raising, because this renders on every authenticated page.
        """
        if self.item.endpoint:
            try:
                return url_for(self.item.endpoint)
            except (BuildError, ValueError):
                return self.item.url or "#"
        return self.item.url or "#"

    @property
    def is_active(self) -> bool:
        if self.item.endpoint and request.endpoint == self.item.endpoint:
            return True
        return any(c.is_active for c in self.children)


def build_menu_for_user(user: User) -> list[MenuNode]:
    user_perms = get_user_permissions(user) if not user.is_superuser else None

    items = MenuItem.query.filter_by(is_visible=True).order_by(MenuItem.order_index).all()

    filtered = [
        m
        for m in items
        if not m.required_permission
        or user_perms is None  # superuser sees all
        or m.required_permission in user_perms
    ]

    tree = _build_tree(_drop_unreachable(filtered))
    return _prune_empty_groups(tree)


def _drop_unreachable(items: list[MenuItem]) -> list[MenuItem]:
    """Drop rows whose endpoint this application does not have.

    Menu rows are data, and data outlives the code that gave it meaning. A
    module can be removed, disabled, or simply not present on the branch
    someone is running -- and then `url_for` raises BuildError, which the
    sidebar had no way to survive. Because the sidebar is on every
    authenticated page, one stale row took the entire signed-in application
    down, including its own admin page for editing menu rows.

    That is not hypothetical. Two branches of this project shared a
    development database; one added a menu row for a module the other did not
    have, and every authenticated page on the other branch answered 500
    (docs/PRELAUNCH.md O13).

    Skipping is the right answer rather than rendering a dead link: the same
    judgement `_prune_empty_groups` already makes for items nobody can click.
    The admin menu page lists rows from the database without resolving them,
    so a skipped row is still visible and fixable there.

    Checked against `view_functions` rather than by trying `url_for`, because
    `url_for` needs a request context and this function does not have to have
    one -- a menu can legitimately be built outside a request.
    """
    known = current_app.view_functions
    kept: list[MenuItem] = []
    skipped: list[str] = []
    for item in items:
        if item.endpoint and item.endpoint not in known:
            skipped.append(item.endpoint)
            continue
        kept.append(item)

    if skipped:
        # Info, not a warning: on a plugin-based app an absent module is a
        # normal state, not a fault. The admin page is where a typo shows up.
        logger.info("menu_endpoint_unresolvable", endpoints=sorted(set(skipped)))
    return kept


def _build_tree(items: list[MenuItem]) -> list[MenuNode]:
    nodes = {m.id: MenuNode(item=m) for m in items}
    roots: list[MenuNode] = []
    for item in items:
        node = nodes[item.id]
        if item.parent_id and item.parent_id in nodes:
            nodes[item.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


def _prune_empty_groups(nodes: list[MenuNode]) -> list[MenuNode]:
    """Drop any node that has no endpoint, no url, AND no visible children.

    A menu item with none of those is just an accordion header (e.g.
    `admin_group`) whose children were all filtered out by the permission
    check above — it renders as a dead, unclickable link, so it should not
    appear at all. Recurses bottom-up so a group left empty by pruning its
    own children is itself pruned. Plain links (endpoint- or url-backed) are
    always kept regardless of children.
    """
    kept: list[MenuNode] = []
    for node in nodes:
        node.children = _prune_empty_groups(node.children)
        if node.item.endpoint or node.item.url or node.children:
            kept.append(node)
    return kept
