import os
import sys

# Force FLASK_ENV to testing before any imports load config
os.environ["FLASK_ENV"] = "testing"

import pytest

from app import create_app
from app.extensions import db as _db


# Live test-progress markers — pytest's own `-v` line only appears AFTER a
# test completes. When something hangs, that line never lands and we lose
# all signal. These hooks print at setup/teardown so the GitHub Actions log
# pinpoints exactly which phase of which test stopped responding.
def pytest_runtest_logstart(nodeid, location):  # noqa: ARG001
    sys.stderr.write(f"\n>>> START {nodeid}\n")
    sys.stderr.flush()


def pytest_runtest_logfinish(nodeid, location):  # noqa: ARG001
    sys.stderr.write(f"<<< DONE  {nodeid}\n")
    sys.stderr.flush()


@pytest.fixture(scope="session")
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    import os

    from app.config import get_config

    # Prefer an explicit TEST_DATABASE_URL (that's what CI sets); fall back to
    # deriving a *_test DB from DATABASE_URL, then to the config default.
    # NOTE: configs expose SQLALCHEMY_DATABASE_URI, not DATABASE_URL — reading
    # the latter off the class raises AttributeError when no env var is set
    # (which is exactly the CI case).
    db_url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or get_config().SQLALCHEMY_DATABASE_URI
    )
    if db_url:
        if db_url.startswith("sqlite:"):
            test_db_url = db_url.replace(".db", "_test.db")
        else:
            base_url, db_name = db_url.rsplit("/", 1)
            if "?" in db_name:
                db_name_only, query_params = db_name.split("?", 1)
                test_db_url = f"{base_url}/scrapemind_test?{query_params}"
            else:
                test_db_url = f"{base_url}/scrapemind_test"
    else:
        test_db_url = "postgresql://scrapemind:scrapemind@localhost:5432/scrapemind_test"

    app.config["SQLALCHEMY_DATABASE_URI"] = test_db_url

    with app.app_context():
        _db.create_all()
        yield app
        # Make sure no transaction holds locks before we DROP — Postgres will
        # block forever on ACCESS EXCLUSIVE if a session is sitting on a row.
        _db.session.rollback()
        _db.session.close()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    with app.app_context():
        yield _db
        _db.session.rollback()


@pytest.fixture
def auth_client(app, db):
    """A test client already logged in as a fresh non-admin user.

    Yields (client, user_id). We create the user, log them in via the
    session-transaction (bypasses CSRF/2FA plumbing), and tear the row down
    afterwards so tests stay isolated.
    """
    from sqlalchemy import text

    from app.core.models.user import User

    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).filter_by(username="httptester").delete()
    db.session.commit()

    from app.core.auth.strategies.local import LocalAuthStrategy

    user = User(
        username="httptester",
        email="httptester@example.test",
        full_name="HTTP Tester",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    uid = user.id

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True

    yield client, uid

    # Tear down child rows first — routes under test write audit_logs,
    # user_papers, notifications, etc. that FK-reference this user, so a bare
    # user delete would raise. Order matters: grandchildren before children.
    db.session.rollback()
    db.session.execute(
        text(
            "DELETE FROM paper_chat_messages WHERE user_paper_id IN "
            "(SELECT id FROM user_papers WHERE user_id = :uid)"
        ),
        {"uid": uid},
    )
    db.session.execute(
        text(
            "DELETE FROM paper_notes WHERE user_paper_id IN "
            "(SELECT id FROM user_papers WHERE user_id = :uid)"
        ),
        {"uid": uid},
    )
    for tbl in (
        "audit_logs",
        "user_papers",
        "notifications",
        "user_settings",
        "oauth_accounts",
        "user_sessions",
        "revoked_tokens",
    ):
        db.session.execute(text(f"DELETE FROM {tbl} WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM user_roles WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()
