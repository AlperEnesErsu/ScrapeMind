from flask import url_for

from app.core.auth.strategies.base import AuthStrategy
from app.core.auth.strategies.oauth_base import resolve_oauth_user
from app.core.models.user import User
from app.extensions import oauth


class GoogleOAuthStrategy(AuthStrategy):
    name = "google"

    def authenticate(self, credentials: dict) -> User | None:
        token = credentials.get("token")
        if not token:
            return None
        userinfo = oauth.google.userinfo(token=token)
        return resolve_oauth_user(
            provider="google",
            provider_user_id=userinfo["sub"],
            email=userinfo.get("email", ""),
            full_name=userinfo.get("name", ""),
            raw_data=userinfo,
        )

    def get_login_url(self) -> str | None:
        redirect_uri = url_for("auth.oauth_callback", provider="google", _external=True)
        return oauth.google.authorize_redirect(redirect_uri)

    @staticmethod
    def register(app):
        oauth.register(
            name="google",
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
