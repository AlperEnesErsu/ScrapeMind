from collections import defaultdict
from datetime import UTC, datetime

from app.core.models.permission import Permission
from app.core.models.role import Role
from app.core.models.user import User
from app.extensions import db


def get_user_permissions(user: User) -> frozenset[str]:
    """Return all permission codes for the user (across all roles)."""
    perms: set[str] = set()
    for role in user.roles:
        if not role.is_deleted:
            for perm in role.permissions:
                perms.add(perm.code)
    return frozenset(perms)


def user_has_permission(user: User, permission_code: str) -> bool:
    """Check a single permission. Superuser bypass is NOT here — see decorators.py."""
    return permission_code in get_user_permissions(user)


def list_roles() -> list[Role]:
    return Role.query.filter(Role.deleted_at.is_(None)).order_by(Role.name).all()


def get_role(role_id: int) -> Role | None:
    role = db.session.get(Role, role_id)
    return role if role and not role.is_deleted else None


def list_permissions_by_module() -> dict[str, list[Permission]]:
    grouped: dict[str, list[Permission]] = defaultdict(list)
    for perm in Permission.query.order_by(Permission.module_code, Permission.code).all():
        grouped[perm.module_code or "core"].append(perm)
    return dict(grouped)


def create_role(name: str, description: str | None, permission_ids: list[int]) -> Role:
    role = Role(name=name.strip(), description=(description or "").strip() or None)
    role.permissions = (
        Permission.query.filter(Permission.id.in_(permission_ids)).all() if permission_ids else []
    )
    db.session.add(role)
    db.session.commit()
    return role


def update_role(role: Role, name: str, description: str | None, permission_ids: list[int]) -> Role:
    role.name = name.strip()
    role.description = (description or "").strip() or None
    role.permissions = (
        Permission.query.filter(Permission.id.in_(permission_ids)).all() if permission_ids else []
    )
    db.session.commit()
    return role


def soft_delete_role(role: Role) -> None:
    role.deleted_at = datetime.now(UTC)
    db.session.commit()
