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

    db_url = os.environ.get("DATABASE_URL") or get_config().DATABASE_URL
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
