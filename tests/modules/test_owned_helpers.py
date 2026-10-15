"""Ownership-checked CRUD helpers shared by feeds / channels / pages / bluesky."""

from __future__ import annotations

import pytest

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.scrape.models import UserFeed
from app.modules.scrape.service._owned import (
    clear_validators,
    count_owned,
    get_owned,
    list_owned,
    remove_owned,
    toggle_owned,
)


@pytest.fixture
def two_users(db):
    users = []
    for name in ("ownedhelper_a", "ownedhelper_b"):
        db.session.query(User).filter_by(username=name).delete()
        u = User(
            username=name,
            email=f"{name}@example.test",
            full_name=name,
            password_hash=LocalAuthStrategy.hash_password("x12345678"),
            is_active=True,
        )
        db.session.add(u)
        users.append(u)
    db.session.commit()
    yield users
    for u in users:
        db.session.query(User).filter_by(id=u.id).delete()  # cascades to user_feeds
    db.session.commit()


def _feed(db, user, url, **kw):
    row = UserFeed(user_id=user.id, url=url, active=kw.pop("active", True), **kw)
    db.session.add(row)
    db.session.commit()
    return row


def test_get_and_count_only_see_the_owners_rows(db, two_users):
    a, b = two_users
    fa = _feed(db, a, "https://example.test/a.xml")
    fb = _feed(db, b, "https://example.test/b.xml")

    assert get_owned(UserFeed, a, fa.id).id == fa.id
    assert get_owned(UserFeed, a, fb.id) is None  # someone else's row
    assert count_owned(UserFeed, a) == 1
    assert [r.id for r in list_owned(UserFeed, b)] == [fb.id]


def test_remove_refuses_another_users_row(db, two_users):
    a, b = two_users
    fb = _feed(db, b, "https://example.test/b.xml")

    assert remove_owned(UserFeed, a, fb.id) is False
    assert UserFeed.query.get(fb.id) is not None
    assert remove_owned(UserFeed, b, fb.id) is True
    assert UserFeed.query.get(fb.id) is None


def test_toggle_flips_active_and_clears_validators_only_on_resume(db, two_users):
    a, b = two_users
    row = _feed(db, a, "https://example.test/a.xml", etag="e1", last_modified="lm1")

    # Pausing keeps the validators; resuming drops them.
    assert toggle_owned(UserFeed, a, row.id, on_resume=clear_validators) is False
    db.session.refresh(row)
    assert (row.etag, row.last_modified) == ("e1", "lm1")

    assert toggle_owned(UserFeed, a, row.id, on_resume=clear_validators) is True
    db.session.refresh(row)
    assert (row.etag, row.last_modified) == (None, None)


def test_toggle_returns_none_for_missing_or_foreign_rows(db, two_users):
    a, b = two_users
    fb = _feed(db, b, "https://example.test/b.xml")
    assert toggle_owned(UserFeed, a, fb.id) is None
    assert toggle_owned(UserFeed, a, 999_999_999) is None
    db.session.refresh(fb)
    assert fb.active is True
