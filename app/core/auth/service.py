from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.role import Role
from app.core.models.user import User
from app.extensions import db

PASSWORD_RESET_SALT = "scrapemind-password-reset"
PASSWORD_RESET_MAX_AGE = 3600  # 1 hour


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=PASSWORD_RESET_SALT)


def register_user(
    *, username: str, email: str, full_name: str, password: str
) -> tuple[User | None, str | None]:
    """Returns (user, error_msgid)."""
    username = username.strip()
    email = email.strip().lower()
    if User.query.filter(User.username == username).first():
        return None, "Username already in use."
    if User.query.filter(User.email == email).first():
        return None, "Email already in use."

    user = User(
        username=username,
        email=email,
        full_name=full_name.strip(),
        password_hash=LocalAuthStrategy.hash_password(password),
        is_active=True,
    )
    default = Role.query.filter_by(name="user", deleted_at=None).first()
    if default is not None:
        user.roles.append(default)
    db.session.add(user)
    db.session.commit()
    return user, None


def make_password_reset_token(user: User) -> str:
    return _serializer().dumps({"uid": user.id, "email": user.email})


def verify_password_reset_token(token: str) -> User | None:
    try:
        data = _serializer().loads(token, max_age=PASSWORD_RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = db.session.get(User, data.get("uid"))
    if user is None or user.is_deleted:
        return None
    # Email rotated since token issued → invalidate
    if user.email != data.get("email"):
        return None
    return user


def reset_password(user: User, new_password: str) -> None:
    user.password_hash = LocalAuthStrategy.hash_password(new_password)
    user.failed_login_count = 0
    user.is_locked = False
    user.locked_until = None
    db.session.commit()
