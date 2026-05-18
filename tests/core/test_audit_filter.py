from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.models.audit import AuditLog


@pytest.fixture
def logs(db):
    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.user import User

    db.session.execute(text("DELETE FROM audit_logs"))
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM oauth_accounts"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()

    u1 = User(
        username="auser1",
        email="a1@test",
        full_name="A1",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    u2 = User(
        username="auser2",
        email="a2@test",
        full_name="A2",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add_all([u1, u2])
    db.session.commit()

    now = datetime.now(UTC)
    db.session.add_all(
        [
            AuditLog(
                action="user.create",
                entity_type="user",
                entity_id=str(u1.id),
                user_id=u1.id,
                created_at=now - timedelta(days=2),
            ),
            AuditLog(
                action="user.update",
                entity_type="user",
                entity_id=str(u1.id),
                user_id=u1.id,
                created_at=now - timedelta(days=1),
            ),
            AuditLog(
                action="role.create",
                entity_type="role",
                entity_id="2",
                user_id=u2.id,
                created_at=now - timedelta(hours=1),
            ),
        ]
    )
    db.session.commit()
    yield {"u1": u1.id, "u2": u2.id}
    db.session.execute(text("DELETE FROM audit_logs"))
    db.session.query(User).delete()
    db.session.commit()


def test_filter_by_user(db, logs):
    rows = AuditLog.query.filter(AuditLog.user_id == logs["u1"]).all()
    assert len(rows) == 2


def test_filter_by_action_prefix(db, logs):
    rows = AuditLog.query.filter(AuditLog.action.ilike("user.%")).all()
    assert len(rows) == 2


def test_filter_by_entity_type(db, logs):
    rows = AuditLog.query.filter(AuditLog.entity_type == "role").all()
    assert len(rows) == 1


def test_audit_route_requires_login(client):
    r = client.get("/admin/audit/", follow_redirects=False)
    assert r.status_code in (302, 401)
