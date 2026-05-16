"""JWT API auth strategy — skeleton for Faz 3.

Planned for service-to-service API auth (Faz 2 API v1 endpoints).
"""

from app.core.auth.strategies.base import AuthStrategy
from app.core.models.user import User


class JwtApiStrategy(AuthStrategy):
    name = "jwt_api"

    def authenticate(self, credentials: dict) -> User | None:
        raise NotImplementedError("JWT auth is planned for Faz 3.")

    def get_login_url(self) -> str | None:
        return None
