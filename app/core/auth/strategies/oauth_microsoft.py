from flask import url_for

from app.core.auth.strategies.base import AuthStrategy
from app.core.auth.strategies.oauth_base import resolve_oauth_user
from app.core.models.user import User
from app.extensions import oauth


class MicrosoftOAuthStrategy(AuthStrategy):
    name = "microsoft"

    def authenticate(self, credentials: dict) -> User | None:
        token = credentials.get("token")
        if not token:
            return None
        userinfo = oauth.microsoft.userinfo(token=token)
        return resolve_oauth_user(
            provider="microsoft",
            provider_user_id=userinfo["sub"],
            email=userinfo.get("email", ""),
            full_name=userinfo.get("name", ""),
            raw_data=userinfo,
        )

    def get_login_url(self) -> str | None:
        redirect_uri = url_for("auth.oauth_callback", provider="microsoft", _external=True)
        return oauth.microsoft.authorize_redirect(redirect_uri)

    @staticmethod
    def register(app):
        tenant = app.config.get("MICROSOFT_TENANT_ID", "common")
        oauth.register(
            name="microsoft",
            client_id=app.config["MICROSOFT_CLIENT_ID"],
            client_secret=app.config["MICROSOFT_CLIENT_SECRET"],
            server_metadata_url=(
                f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
            ),
            client_kwargs={"scope": "openid email profile"},
        )
