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

    return _build_tree(filtered)


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
