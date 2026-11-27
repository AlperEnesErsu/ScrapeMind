"""Out-of-browser steps for the end-to-end smoke suite (e2e/).

    python scripts/e2e_helper.py prepare
    python scripts/e2e_helper.py wait-worker
    python scripts/e2e_helper.py run-alerts <username> [cadence]
    python scripts/e2e_helper.py reset-limits

Run by scripts/e2e.py and by the specs themselves, never against a database
anyone cares about: `prepare` creates users with known passwords.

`run-alerts` is the reason this file exists. A saved-search alert runs on the
beat schedule, in a worker process with no request, and Faz 7.1 shipped dead
for exactly that reason -- every test called the function inside a request
context the worker never has. So the task is sent through the real broker to
the real worker, and the spec then reads the result in the browser.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: (username, full name). The password is shared and deliberately not a secret:
#: these accounts exist only in a throwaway e2e database.
USERS = {
    "e2e-reader": "E2E Reader",
    "e2e-researcher": "E2E Researcher",
    "e2e-polyglot": "E2E Polyglot",
}
PASSWORD = "e2e-password-1234"  # noqa: S105


def prepare() -> None:
    """Seed the database and create one user per spec that writes state.

    One user per writer keeps the specs independent: the interests spec cannot
    change what the alert spec's user sees, whatever order they run in.
    """
    from app import create_app
    from app.core.auth.strategies.local import LocalAuthStrategy
    from app.core.models.role import Role
    from app.core.models.user import User
    from app.extensions import db
    from scripts.seed import run as run_seed

    app = create_app()
    run_seed(app)
    with app.app_context():
        role = Role.query.filter_by(name="user").one()
        for username, full_name in USERS.items():
            if User.query.filter_by(username=username).first() is not None:
                continue
            user = User(
                username=username,
                email=f"{username}@example.test",
                full_name=full_name,
                password_hash=LocalAuthStrategy.hash_password(PASSWORD),
                is_active=True,
            )
            user.roles.append(role)
            db.session.add(user)
        db.session.commit()
    print(f"prepared: admin + {', '.join(USERS)}")


def wait_worker(timeout: float = 90.0) -> None:
    """Block until a worker answers a ping, so no spec races its startup."""
    from app import create_app
    from app.tasks import celery_app

    create_app()  # binds the broker from config, as the web process does
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if celery_app.control.ping(timeout=1.0):
            print("worker ready")
            return
        time.sleep(1)
    raise SystemExit(f"no Celery worker answered within {timeout:.0f}s")


def run_alerts(username: str, cadence: str = "weekly") -> None:
    """Send `alerts.run_for_user` to the worker and print its result as JSON."""
    from app import create_app
    from app.core.models.user import User
    from app.tasks import celery_app

    app = create_app()
    with app.app_context():
        user = User.query.filter_by(username=username).one()
        user_id = user.id

    # send_task by name, not `run_for_user.delay`: nothing about this call may
    # be able to execute the task in this process.
    result = celery_app.send_task("alerts.run_for_user", args=[user_id, cadence])
    print(json.dumps(result.get(timeout=60)))


def reset_limits() -> None:
    """Clear the rate-limit counters so one spec's sign-ins do not starve the next.

    scripts/e2e.py gives the limiter a Redis database of its own, so flushing
    it touches nothing else the run depends on.
    """
    import os

    import redis

    uri = os.environ["RATELIMIT_STORAGE_URI"]
    redis.Redis.from_url(uri).flushdb()
    print(json.dumps({"flushed": uri}))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, args = argv[0], argv[1:]
    if command == "prepare":
        prepare()
    elif command == "wait-worker":
        wait_worker()
    elif command == "reset-limits":
        reset_limits()
    elif command == "run-alerts":
        run_alerts(*args)
    else:
        print(f"unknown command: {command}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
