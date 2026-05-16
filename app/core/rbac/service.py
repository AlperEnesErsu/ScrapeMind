from functools import lru_cache

from app.core.models.user import User


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
