from __future__ import annotations

from dataclasses import dataclass, field

from flask import request

from app.core.models.menu import MenuItem
from app.core.models.user import User
from app.core.rbac.service import get_user_permissions


@dataclass
class MenuNode:
    item: MenuItem
    children: list[MenuNode] = field(default_factory=list)

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

    tree = _build_tree(filtered)
    return _prune_empty_groups(tree)


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
