from datetime import UTC, datetime

from flask import request

from app.core.models.session import UserSession
from app.core.models.user import User
from app.extensions import db

_SESSION_KEY_COOKIE = "_sk"


def create_session(user: User) -> str:
    """Login sonrası çağrılır. DB kaydı oluşturur, key döner."""
    key = UserSession.generate_key()
    record = UserSession(
        user_id=user.id,
        session_key=key,
        ip_address=request.remote_addr,
        user_agent=(request.user_agent.string or "")[:255],
    )
    db.session.add(record)
    db.session.commit()
    return key


def touch_session(key: str, expected_user_id: int | None = None) -> UserSession | None:
    """Her istekte çağrılır — last_seen günceller, bulamazsa None döner.

    `expected_user_id` verilirse (auth'lı istekte current_user.id), session
    kaydının sahibiyle eşleşmezse None döner — böylece bir kullanıcının
    cookie'sindeki session key başka bir kullanıcının kaydına denk gelirse
    (defense-in-depth) oturum geçersiz sayılır.
    """
    record = UserSession.query.filter_by(session_key=key).first()
    if record is None:
        return None
    if expected_user_id is not None and record.user_id != expected_user_id:
        return None
    record.last_seen_at = datetime.now(UTC)
    db.session.commit()
    return record


def delete_session(key: str) -> None:
    """Logout veya bireysel revoke."""
    record = UserSession.query.filter_by(session_key=key).first()
    if record:
        db.session.delete(record)
        db.session.commit()


def delete_all_sessions(user: User, except_key: str | None = None) -> int:
    """Tüm oturumları kapat; except_key mevcut oturumu korur."""
    q = UserSession.query.filter_by(user_id=user.id)
    if except_key:
        q = q.filter(UserSession.session_key != except_key)
    count = q.count()
    q.delete()
    db.session.commit()
    return count


def list_sessions(user: User) -> list[UserSession]:
    return (
        UserSession.query.filter_by(user_id=user.id).order_by(UserSession.last_seen_at.desc()).all()
    )


def get_current_key() -> str | None:
    from flask import session

    return session.get(_SESSION_KEY_COOKIE)


def set_current_key(key: str) -> None:
    from flask import session

    session[_SESSION_KEY_COOKIE] = key


def clear_current_key() -> None:
    from flask import session

    session.pop(_SESSION_KEY_COOKIE, None)
