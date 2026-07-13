"""JWT API auth strategy placeholder.

The live JWT bearer-token API lives in `app/api/v1` (see docs/API_V1.md); it
issues/verifies tokens directly via `app/api/v1/tokens.py` rather than through
this pluggable web-login strategy. This class stays as a no-op so the strategy
registry has an entry, should we later want interactive JWT login too.
"""

from app.core.auth.strategies.base import AuthStrategy
from app.core.models.user import User


class JwtApiStrategy(AuthStrategy):
    name = "jwt_api"

    def authenticate(self, credentials: dict) -> User | None:
        # Token issuance/verification is handled by app/api/v1, not here.
        raise NotImplementedError("Use the /api/v1/auth/token endpoint for JWT auth.")

    def get_login_url(self) -> str | None:
        return None
