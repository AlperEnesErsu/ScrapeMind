from datetime import UTC, datetime

from sqlalchemy import or_

from app.core.models.role import Role
from app.core.models.user import User
from app.extensions import db


def list_users(*, query: str | None = None, only_active: bool = False) -> list[User]:
    q = User.query.filter(User.deleted_at.is_(None))
    if query:
        like = f"%{query.strip()}%"
        q = q.filter(
            or_(User.username.ilike(like), User.email.ilike(like), User.full_name.ilike(like))
        )
    if only_active:
        q = q.filter(User.is_active.is_(True))
    return q.order_by(User.username).all()


def get_user(user_id: int) -> User | None:
    u = db.session.get(User, user_id)
    return u if u and not u.is_deleted else None


def update_user(
    user: User,
    *,
    full_name: str,
    email: str,
    is_active: bool,
    is_superuser: bool,
    avatar_url: str | None,
) -> tuple[bool, str | None]:
    email = email.strip().lower()
    if email != user.email and User.query.filter(User.email == email, User.id != user.id).first():
        return False, "Email already in use."
    user.full_name = full_name.strip()
    user.email = email
    user.is_active = is_active
    user.is_superuser = is_superuser
    user.avatar_url = (avatar_url or "").strip() or None
    db.session.commit()
    return True, None


def set_user_roles(user: User, role_ids: list[int]) -> None:
    user.roles = (
        Role.query.filter(Role.id.in_(role_ids), Role.deleted_at.is_(None)).all()
        if role_ids
        else []
    )
    db.session.commit()


def lock_user(user: User) -> None:
    user.is_locked = True
    user.locked_until = None  # indefinite lock until admin unlocks
    db.session.commit()


def unlock_user(user: User) -> None:
    user.is_locked = False
    user.locked_until = None
    user.failed_login_count = 0
    db.session.commit()


def soft_delete_user(user: User) -> None:
    user.deleted_at = datetime.now(UTC)
    user.is_active = False
    db.session.commit()
