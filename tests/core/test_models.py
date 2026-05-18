from datetime import UTC, datetime, timedelta


def test_user_locked_until_auto_clears():
    from app.core.models.user import User

    u = User()
    u.is_locked = True
    u.locked_until = datetime.now(UTC) - timedelta(seconds=1)  # expired
    assert u.effective_is_locked is False


def test_user_locked_still_active():
    from app.core.models.user import User

    u = User()
    u.is_locked = True
    u.locked_until = datetime.now(UTC) + timedelta(minutes=14)
    assert u.effective_is_locked is True


def test_base_model_soft_delete():
    from app.core.models.user import User

    u = User()
    assert u.is_deleted is False
    u.soft_delete()
    assert u.is_deleted is True
    u.restore()
    assert u.is_deleted is False
