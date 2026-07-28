"""Per-user fan-out with a deterministic time offset.

The three nightly `*_for_all_users` tasks used to do::

    for u in User.query.filter_by(...).all():
        task.delay(u.id)

which materialises every user object and enqueues the whole population in the
same instant. At a few hundred users that is a thundering herd against shared
external APIs; at a few thousand it is also a memory problem.

`fan_out` streams user ids and gives each one a `countdown` derived from the
id itself. Deterministic rather than random on purpose: the same user is
scanned at the same minute every night, which is what lets the UI tell them
"next scan 03:27" and be telling the truth (see
`app/tasks/schedule_info.py:next_scan_at_for_user`).

Scale note: `countdown` tasks are held in a worker's in-memory ETA set. That
is fine into the low thousands of users; past that, replace this with a
scheduler task that re-enqueues in chunks.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from flask import current_app

from app.core.models.user import User
from app.extensions import db

logger = structlog.get_logger()

# Knuth's multiplicative hash constant — spreads sequential ids evenly rather
# than mapping users 1..N onto the first N seconds of the window.
_KNUTH = 2654435761


def user_fanout_offset(user_id: int, window_seconds: int | None = None) -> int:
    """Seconds after the schedule's fire time at which this user is scanned.

    Stable for a given (user_id, window): the value is a pure function of the
    id, so it survives restarts, redeploys and beat failovers.
    """
    if window_seconds is None:
        window_seconds = int(current_app.config.get("SCAN_FANOUT_WINDOW_SECONDS", 1800))
    if window_seconds <= 0:
        return 0
    return (int(user_id) * _KNUTH) % window_seconds


def active_user_ids():
    """Stream ids of users eligible for scheduled work."""
    q = (
        db.session.query(User.id)
        .filter(User.deleted_at.is_(None), User.is_active.is_(True))
        .order_by(User.id)
    )
    for (uid,) in q.yield_per(1000):
        yield uid


def fan_out(
    task,
    *,
    args_for: Callable[[int], tuple] = lambda uid: (uid,),
    kwargs_for: Callable[[int], dict] | None = None,
    window_seconds: int | None = None,
) -> int:
    """Queue `task` once per active user, spread over the fan-out window.

    Returns the number of tasks queued.
    """
    if window_seconds is None:
        window_seconds = int(current_app.config.get("SCAN_FANOUT_WINDOW_SECONDS", 1800))
    queued = 0
    for uid in active_user_ids():
        task.apply_async(
            args=args_for(uid),
            kwargs=kwargs_for(uid) if kwargs_for else None,
            countdown=user_fanout_offset(uid, window_seconds),
        )
        queued += 1
    logger.info("fanout_queued", task=task.name, queued=queued, window=window_seconds)
    return queued
