import sys

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
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        "postgresql://scrapemind:scrapemind@localhost:5432/scrapemind_test"
    )
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
