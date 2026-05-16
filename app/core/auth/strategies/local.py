from datetime import datetime, timedelta, timezone

import structlog
from passlib.context import CryptContext

from app.core.auth.strategies.base import AuthStrategy
from app.core.models.user import User
from app.extensions import db

logger = structlog.get_logger()

_pwd_ctx = CryptContext(schemes=["argon2"], deprecated="auto")

FAILED_LOGIN_LIMIT = 5
LOCKOUT_MINUTES = 15


class LocalAuthStrategy(AuthStrategy):
    name = "local"

    def authenticate(self, credentials: dict) -> User | None:
        username_or_email: str = credentials.get("username", "")
        password: str = credentials.get("password", "")

        user = User.query.filter(
            (User.username == username_or_email) | (User.email == username_or_email),
            User.deleted_at.is_(None),
        ).first()

        if user is None:
            return None

        user.check_lock()

        if user.effective_is_locked:
            logger.warning("login_blocked_locked", user_id=user.id)
            return None

        if not user.is_active:
            return None

        if not _pwd_ctx.verify(password, user.password_hash or ""):
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= FAILED_LOGIN_LIMIT:
                user.is_locked = True
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
                logger.warning("user_locked_brute_force", user_id=user.id)
            db.session.commit()
            return None

        user.failed_login_count = 0
        user.is_locked = False
        user.locked_until = None
        user.last_login_at = datetime.now(timezone.utc)
        db.session.commit()
        return user

    def get_login_url(self) -> str | None:
        return None

    @staticmethod
    def hash_password(plain: str) -> str:
        return _pwd_ctx.hash(plain)

    @staticmethod
    def verify_password(plain: str, hashed: str) -> bool:
        if not hashed:
            return False
        return _pwd_ctx.verify(plain, hashed)
