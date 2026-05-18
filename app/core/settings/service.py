from typing import Any

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.settings import SystemSettings, UserSettings
from app.core.models.user import User
from app.extensions import db


def get_system_setting(key: str, default: Any = None) -> Any:
    row = SystemSettings.query.get(key)
    return row.value if row else default


def set_system_setting(key: str, value: Any, updated_by_id: int | None = None) -> None:
    row = SystemSettings.query.get(key)
    if row:
        row.value = value
        row.updated_by = updated_by_id
    else:
        row = SystemSettings(key=key, value=value, updated_by=updated_by_id)
        db.session.add(row)
    db.session.commit()


def update_personal_info(user: User, full_name: str, avatar_url: str | None) -> None:
    user.full_name = full_name.strip()
    user.avatar_url = (avatar_url or "").strip() or None
    db.session.commit()


def update_email(user: User, new_email: str, current_password: str) -> tuple[bool, str | None]:
    """Change the primary email.

    Now goes through the academic.UserIdentifier table so the multi-email
    model stays in sync. If the new address isn't already on the user, it
    gets added and verified-in-one-step (user just typed their password).
    """
    if user.password_hash and not LocalAuthStrategy.verify_password(
        current_password, user.password_hash
    ):
        return False, "Current password is incorrect."
    new_email = new_email.strip().lower()
    if new_email == user.email:
        return True, None

    # Imported here to avoid a circular import at module load: academic.service
    # eventually depends on app.core.models which is reached through settings.
    from app.modules.academic.models import UserIdentifier
    from app.modules.academic.service import add_email, get_email_type, set_primary_email

    et = get_email_type()
    existing = UserIdentifier.query.filter_by(
        user_id=user.id, identifier_type_id=et.id, value=new_email
    ).first()
    if existing is None:
        # Password just re-confirmed identity — auto-verify in this flow.
        existing, err = add_email(user, new_email, verified=True)
        if err:
            return False, err
    elif not existing.is_verified:
        existing.is_verified = True
        from datetime import UTC, datetime

        existing.verified_at = datetime.now(UTC)
        db.session.commit()
    return set_primary_email(user, existing.id)


def change_password(
    user: User, current_password: str, new_password: str
) -> tuple[bool, str | None]:
    if not user.password_hash:
        return False, "Local password not set for this account."
    if not LocalAuthStrategy.verify_password(current_password, user.password_hash):
        return False, "Current password is incorrect."
    user.password_hash = LocalAuthStrategy.hash_password(new_password)
    db.session.commit()
    return True, None


def update_preferences(user: User, locale: str, timezone: str, theme: str) -> None:
    user.locale = locale
    user.timezone = timezone
    settings = user.settings
    if settings is None:
        settings = UserSettings(user_id=user.id, settings={})
        db.session.add(settings)
    copy = dict(settings.settings or {})
    copy["theme"] = theme
    settings.settings = copy
    db.session.commit()


def get_theme(user: User) -> str:
    if user.settings and user.settings.settings:
        return user.settings.settings.get("theme", "light")
    return "light"
