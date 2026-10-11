from collections import defaultdict
from datetime import UTC, datetime

from app.core import cache
from app.core.models.permission import Permission
from app.core.models.role import Role, role_permissions
from app.core.models.user import User, user_roles
from app.extensions import db

# Namespace for the permission cache. Any RBAC write bumps its version, which
# retires every cached permission set at once — see app/core/cache.py.
PERM_NAMESPACE = "rbac"


def _load_user_permissions(user: User) -> frozenset[str]:
    """Read permission codes straight from the DB in one join.

    Walking user.roles → role.permissions in Python costs 1 + N queries (both
    relationships are lazy="select"); this is a single indexed join instead.
    """
    rows = (
        db.session.query(Permission.code)
        .join(role_permissions, role_permissions.c.permission_id == Permission.id)
        .join(Role, Role.id == role_permissions.c.role_id)
        .join(user_roles, user_roles.c.role_id == Role.id)
        .filter(user_roles.c.user_id == user.id, Role.deleted_at.is_(None))
        .distinct()
        .all()
    )
    return frozenset(code for (code,) in rows)


def get_user_permissions(user: User) -> frozenset[str]:
    """All permission codes for the user, across all their (live) roles.

    Cached in Redis when available — this runs on every authenticated request
    via the menu context processor. A cache miss, or no Redis at all, just
    reads through to the DB.
    """
    version = cache.get_version(PERM_NAMESPACE)
    if version is None:
        # No cache available — don't invent a version, just read through.
        return _load_user_permissions(user)

    key = f"perms:v{version}:u{user.id}"
    cached = cache.get_json(key)
    if cached is not None:
        return frozenset(cached)

    perms = _load_user_permissions(user)
    cache.set_json(key, sorted(perms))
    return perms


def invalidate_permission_cache() -> None:
    """Call after any change to roles, role permissions, or role assignment."""
    cache.bump_version(PERM_NAMESPACE)


def user_has_permission(user: User, permission_code: str) -> bool:
    """Check a single permission. Superuser bypass is NOT here — see decorators.py."""
    return permission_code in get_user_permissions(user)


def ungrantable_codes(actor: User, codes) -> list[str]:
    """The permission codes in `codes` that `actor` may not grant or revoke.

    `users.manage` and `roles.manage` exist so the work can be delegated to a
    role that is not a superuser. Without this check a delegate could hand
    any permission to anyone -- themselves included -- and "may manage
    users" became "is a superuser". The rule: you can grant or take away only
    what you could use yourself.

    A superuser can use every permission (the bypass in
    `permission_required`), so nothing is out of their reach. This does not
    add a second bypass; it asks the same question that one answers.
    """
    if actor.is_superuser:
        return []
    return sorted(set(codes) - get_user_permissions(actor))


def role_permission_codes(role_ids) -> set[str]:
    """Every permission code carried by the given (live) roles."""
    if not role_ids:
        return set()
    rows = (
        db.session.query(Permission.code)
        .join(role_permissions, role_permissions.c.permission_id == Permission.id)
        .join(Role, Role.id == role_permissions.c.role_id)
        .filter(Role.id.in_(list(role_ids)), Role.deleted_at.is_(None))
        .distinct()
        .all()
    )
    return {code for (code,) in rows}


def permission_codes(permission_ids) -> set[str]:
    if not permission_ids:
        return set()
    rows = db.session.query(Permission.code).filter(Permission.id.in_(list(permission_ids))).all()
    return {code for (code,) in rows}


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
    invalidate_permission_cache()
    return role


def update_role(role: Role, name: str, description: str | None, permission_ids: list[int]) -> Role:
    role.name = name.strip()
    role.description = (description or "").strip() or None
    role.permissions = (
        Permission.query.filter(Permission.id.in_(permission_ids)).all() if permission_ids else []
    )
    db.session.commit()
    # Changing a role's permissions changes the effective permissions of every
    # user holding it — hence a namespace-wide bump, not a per-user delete.
    invalidate_permission_cache()
    return role


def soft_delete_role(role: Role) -> None:
    role.deleted_at = datetime.now(UTC)
    db.session.commit()
    invalidate_permission_cache()
