from app.core.models.audit import AuditLog
from app.core.models.menu import MenuItem
from app.core.models.module import Module
from app.core.models.oauth_account import OAuthAccount
from app.core.models.permission import Permission
from app.core.models.role import Role
from app.core.models.settings import SystemSettings, UserSettings
from app.core.models.user import User

# Module-owned models are imported here so Alembic and SQLAlchemy registry
# see them. PROJECT.md keeps core/ free of module-specific code, but model
# *registration* is unavoidable at this layer until a plugin-aware Alembic
# env.py is built (planned for late Phase 2).
from app.modules.academic.models import (  # noqa: E402
    IdentifierType,
    Keyword,
    UserIdentifier,
    UserKeyword,
)

__all__ = [
    "Module",
    "Role",
    "Permission",
    "MenuItem",
    "OAuthAccount",
    "User",
    "UserSettings",
    "SystemSettings",
    "AuditLog",
    "IdentifierType",
    "UserIdentifier",
    "Keyword",
    "UserKeyword",
]
