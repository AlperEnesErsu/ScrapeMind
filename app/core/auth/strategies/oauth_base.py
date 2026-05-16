"""Shared OAuth → User resolution logic (provider-agnostic)."""

import structlog

from app.core.models.oauth_account import OAuthAccount
from app.core.models.user import User
from app.core.settings.service import get_system_setting
from app.extensions import db

logger = structlog.get_logger()


def resolve_oauth_user(
    *,
    provider: str,
    provider_user_id: str,
    email: str,
    full_name: str,
    raw_data: dict,
) -> User | None:
    # 1. Existing OAuth account
    account = OAuthAccount.query.filter_by(
        provider=provider, provider_user_id=provider_user_id
    ).first()
    if account:
        return account.user

    # 2. Existing user with matching email — link OAuth account
    user = User.query.filter_by(email=email).filter(User.deleted_at.is_(None)).first()
    if user:
        _link_account(user, provider, provider_user_id, email, raw_data)
        return user

    # 3. Auto-register if allowed
    if not get_system_setting("oauth_auto_register", default=False):
        logger.warning("oauth_auto_register_disabled", provider=provider, email=email)
        return None

    user = _create_user(email, full_name)
    _link_account(user, provider, provider_user_id, email, raw_data)
    return user


def _link_account(user, provider, provider_user_id, email, raw_data):
    account = OAuthAccount(
        user_id=user.id,
        provider=provider,
        provider_user_id=provider_user_id,
        email=email,
        raw_data=raw_data,
    )
    db.session.add(account)
    db.session.commit()


def _create_user(email: str, full_name: str) -> User:
    username = email.split("@")[0]
    user = User(username=username, email=email, full_name=full_name)
    db.session.add(user)
    db.session.flush()
    _link_account  # called by caller
    db.session.commit()
    return user
