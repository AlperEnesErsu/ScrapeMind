"""Cache layer + permission caching.

No real Redis here (CI has none) — a fake in-memory client stands in, which
also lets us simulate the failure modes that matter: Redis down, Redis
throwing mid-call, corrupt entries.
"""

import pytest
from sqlalchemy import text

from app.core import cache
from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.permission import Permission
from app.core.models.role import Role
from app.core.models.user import User
from app.core.rbac.service import (
    PERM_NAMESPACE,
    get_user_permissions,
    invalidate_permission_cache,
    update_role,
)


class FakeRedis:
    """Minimal stand-in: get/setex/incr/ping over a dict."""

    def __init__(self, broken: bool = False):
        self.store: dict[str, str] = {}
        self.broken = broken
        self.gets = 0

    def _boom(self):
        if self.broken:
            raise ConnectionError("redis is down")

    def ping(self):
        self._boom()
        return True

    def get(self, key):
        self._boom()
        self.gets += 1
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self._boom()
        self.store[key] = value

    def incr(self, key):
        self._boom()
        self.store[key] = str(int(self.store.get(key, 0)) + 1)


@pytest.fixture
def fake_cache(app, monkeypatch):
    """Wire a FakeRedis in and turn caching on for this test."""
    fake = FakeRedis()
    app.config["CACHE_ENABLED"] = True
    cache.reset_client()
    monkeypatch.setattr(cache, "_redis", lambda: fake)
    yield fake
    app.config["CACHE_ENABLED"] = False
    cache.reset_client()


@pytest.fixture
def rbac_user(db):
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.execute(text("DELETE FROM role_permissions"))
    db.session.query(Role).filter(Role.name.in_(["cache_role"])).delete(synchronize_session=False)
    db.session.query(User).filter_by(username="cacheuser").delete()
    db.session.query(Permission).filter(Permission.code.in_(["cache.read", "cache.write"])).delete(
        synchronize_session=False
    )
    db.session.commit()

    # module_code is an FK to modules.code — leave it null rather than depend
    # on plugin discovery having run.
    p1 = Permission(code="cache.read", label_key="perm.cache.read")
    p2 = Permission(code="cache.write", label_key="perm.cache.write")
    db.session.add_all([p1, p2])
    role = Role(name="cache_role")
    db.session.add(role)
    db.session.commit()
    role.permissions = [p1]
    u = User(
        username="cacheuser",
        email="cacheuser@example.test",
        full_name="Cache User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(u)
    db.session.commit()
    u.roles = [role]
    db.session.commit()

    yield {"user": u, "role": role, "p2_id": p2.id, "p1_id": p1.id}

    db.session.rollback()
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.execute(text("DELETE FROM role_permissions"))
    db.session.query(User).filter_by(username="cacheuser").delete()
    db.session.query(Role).filter(Role.name.in_(["cache_role"])).delete(synchronize_session=False)
    db.session.query(Permission).filter(Permission.code.in_(["cache.read", "cache.write"])).delete(
        synchronize_session=False
    )
    db.session.commit()


# ---------------------------------------------------------------------------
# Cache primitives
# ---------------------------------------------------------------------------


def test_roundtrip(app, fake_cache):
    cache.set_json("k", {"a": 1})
    assert cache.get_json("k") == {"a": 1}


def test_miss_returns_none(app, fake_cache):
    assert cache.get_json("nope") is None


def test_corrupt_entry_reads_as_miss(app, fake_cache):
    fake_cache.store["bad"] = "{not json"
    assert cache.get_json("bad") is None


def test_version_starts_at_zero_and_bumps(app, fake_cache):
    assert cache.get_version("ns") == 0
    cache.bump_version("ns")
    assert cache.get_version("ns") == 1


def test_disabled_cache_is_inert(app, monkeypatch):
    app.config["CACHE_ENABLED"] = False
    cache.reset_client()
    cache.set_json("k", 1)
    assert cache.get_json("k") is None
    assert cache.get_version("ns") is None  # signals "read through"
    cache.bump_version("ns")  # must not raise


def test_broken_redis_degrades_silently(app, monkeypatch):
    """A cache outage must slow us down, not break us."""
    app.config["CACHE_ENABLED"] = True
    cache.reset_client()
    monkeypatch.setattr(cache, "_redis", lambda: FakeRedis(broken=True))
    assert cache.get_json("k") is None
    assert cache.get_version("ns") is None
    cache.set_json("k", 1)  # must not raise
    cache.bump_version("ns")  # must not raise
    app.config["CACHE_ENABLED"] = False
    cache.reset_client()


# ---------------------------------------------------------------------------
# Permission caching
# ---------------------------------------------------------------------------


def test_permissions_are_correct_without_cache(db, rbac_user):
    assert get_user_permissions(rbac_user["user"]) == frozenset({"cache.read"})


def test_permissions_are_cached_and_served_from_redis(db, rbac_user, fake_cache):
    user = rbac_user["user"]
    assert get_user_permissions(user) == frozenset({"cache.read"})
    keys = [k for k in fake_cache.store if k.startswith("perms:")]
    assert len(keys) == 1

    # Second call must not touch the DB — prove it by poisoning the cache with
    # a value the DB would never return.
    fake_cache.store[keys[0]] = '["poisoned"]'
    assert get_user_permissions(user) == frozenset({"poisoned"})


def test_updating_role_permissions_invalidates(db, rbac_user, fake_cache):
    user = rbac_user["user"]
    assert get_user_permissions(user) == frozenset({"cache.read"})

    update_role(rbac_user["role"], "cache_role", None, [rbac_user["p1_id"], rbac_user["p2_id"]])

    # The version bump must retire the old entry, so the new permission shows up.
    assert get_user_permissions(user) == frozenset({"cache.read", "cache.write"})


def test_invalidate_bumps_the_namespace(app, fake_cache):
    before = cache.get_version(PERM_NAMESPACE)
    invalidate_permission_cache()
    assert cache.get_version(PERM_NAMESPACE) == before + 1


def test_soft_deleted_role_grants_nothing(db, rbac_user):
    from app.core.rbac.service import soft_delete_role

    soft_delete_role(rbac_user["role"])
    assert get_user_permissions(rbac_user["user"]) == frozenset()
