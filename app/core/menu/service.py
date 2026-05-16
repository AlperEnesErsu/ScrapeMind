from flask import current_app

from app.core.models.menu import MenuItem
from app.core.models.module import Module
from app.core.models.permission import Permission
from app.extensions import db

CRITICAL_CODES = {
    "dashboard_root",
    "admin_roles",
    "admin_permissions",
    "admin_menu",
    "admin_audit",
    "settings_profile",
}


def is_critical(item: MenuItem) -> bool:
    return item.code in CRITICAL_CODES


def list_items() -> list[MenuItem]:
    return MenuItem.query.order_by(MenuItem.order_index, MenuItem.code).all()


def get_item(item_id: int) -> MenuItem | None:
    return db.session.get(MenuItem, item_id)


def parent_choices(exclude_id: int | None = None) -> list[tuple[int, str]]:
    """Choices for parent dropdown: (id, label). Excludes the item itself."""
    items = MenuItem.query.order_by(MenuItem.code).all()
    return [(0, "—")] + [(i.id, i.code) for i in items if i.id != exclude_id]


def module_choices() -> list[tuple[str, str]]:
    return [("", "—")] + [(m.code, m.code) for m in Module.query.order_by(Module.code).all()]


def permission_choices() -> list[tuple[str, str]]:
    return [("", "—")] + [
        (p.code, p.code) for p in Permission.query.order_by(Permission.code).all()
    ]


def endpoint_exists(endpoint: str) -> bool:
    return endpoint in current_app.view_functions


def has_cycle(item_id: int | None, new_parent_id: int | None) -> bool:
    """Walk up from new_parent; if we hit item_id, cycle."""
    if not new_parent_id or not item_id:
        return False
    current_id = new_parent_id
    seen: set[int] = set()
    while current_id:
        if current_id == item_id:
            return True
        if current_id in seen:
            return True  # existing cycle in data — abort
        seen.add(current_id)
        parent = db.session.get(MenuItem, current_id)
        if not parent:
            return False
        current_id = parent.parent_id
    return False


def create_item(data: dict) -> MenuItem:
    item = MenuItem(**_normalize(data, new=True))
    db.session.add(item)
    db.session.commit()
    return item


def update_item(item: MenuItem, data: dict) -> MenuItem:
    for k, v in _normalize(data, new=False).items():
        setattr(item, k, v)
    db.session.commit()
    return item


def hard_delete(item: MenuItem) -> None:
    item.roles = []  # clear role_menus FKs
    db.session.delete(item)
    db.session.commit()


def toggle_visible(item: MenuItem) -> bool:
    item.is_visible = not item.is_visible
    db.session.commit()
    return item.is_visible


def _normalize(data: dict, *, new: bool) -> dict:
    parent_id = data.get("parent_id")
    parent_id = int(parent_id) if parent_id and int(parent_id) != 0 else None
    out = {
        "code": data["code"].strip(),
        "label_key": data["label_key"].strip(),
        "icon": (data.get("icon") or "").strip() or None,
        "url": (data.get("url") or "").strip() or None,
        "endpoint": (data.get("endpoint") or "").strip() or None,
        "module_code": (data.get("module_code") or "").strip() or None,
        "required_permission": (data.get("required_permission") or "").strip() or None,
        "order_index": int(data.get("order_index") or 0),
        "is_visible": bool(data.get("is_visible")),
        "parent_id": parent_id,
    }
    if not new:
        out.pop("code", None)  # code is immutable after creation (used by seed idempotency)
    return out
