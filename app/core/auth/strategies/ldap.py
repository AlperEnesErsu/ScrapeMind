"""LDAP auth strategy — skeleton for Faz 3.

Implementation is intentionally empty to demonstrate how easily a new
strategy plugs into the AuthStrategy pattern without touching any other code.
"""

from app.core.auth.strategies.base import AuthStrategy
from app.core.models.user import User


class LdapAuthStrategy(AuthStrategy):
    name = "ldap"

    def authenticate(self, credentials: dict) -> User | None:
        raise NotImplementedError("LDAP authentication is planned for Faz 3.")

    def get_login_url(self) -> str | None:
        return None
