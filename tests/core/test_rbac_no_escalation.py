"""A delegated administrator cannot hand out more than they hold.

`users.manage` and `roles.manage` exist so the work can be delegated to a role
that is not a superuser. Before this, such a delegate could:

- tick "superuser" on their own account, or on a new one;
- give any user -- themselves included -- a role carrying permissions they
  did not have (the admin role, say);
- add any permission to any role, their own included;
- demote, lock or delete an actual superuser.

Each of those turns "may manage users" into "is a superuser". The rule now:
a non-superuser may grant or revoke only permissions they hold, may not touch
the superuser flag, and may not change a superuser's account.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.models.permission import Permission
from app.core.models.role import Role
from app.core.models.user import User


def _perm(db, code):
    perm = Permission.query.filter_by(code=code).first()
    if perm is None:
        perm = Permission(code=code, label_key=code)
        db.session.add(perm)
        db.session.flush()
    return perm


@pytest.fixture
def world(app, db, auth_client):
    """The delegate (auth_client's user, not a superuser) holding
    users.view/users.manage/roles.view/roles.manage via one role; a powerful
    role carrying audit.view; a superuser; and a plain victim."""
    client, uid = auth_client
    made_roles, made_users = [], []

    perms = {
        code: _perm(db, code)
        for code in ("users.view", "users.manage", "roles.view", "roles.manage", "audit.view")
    }
    delegate_role = Role(name="delegate", description="t")
    delegate_role.permissions = [perms[c] for c in perms if c != "audit.view"]
    powerful = Role(name="powerful", description="t")
    powerful.permissions = [perms["audit.view"]]
    db.session.add_all([delegate_role, powerful])
    db.session.flush()
    made_roles += [delegate_role.id, powerful.id]

    me = db.session.get(User, uid)
    me.is_superuser = False
    # auth_client's address is @example.test, which the edit form's Email()
    # validator rejects -- every self-edit would fail validation and these
    # tests would pass without the guard being reached.
    me.email = "delegate@example.com"
    me.roles = [delegate_role]

    from app.core.auth.strategies.local import LocalAuthStrategy

    def mk(name, superuser=False):
        u = User(
            username=name,
            email=f"{name}@example.com",
            full_name=name.title(),
            password_hash=LocalAuthStrategy.hash_password("x12345678"),
            is_active=True,
            is_superuser=superuser,
        )
        db.session.add(u)
        db.session.flush()
        made_users.append(u.id)
        return u

    boss = mk("bossuser", superuser=True)
    victim = mk("victimuser")
    db.session.commit()

    from app.core.rbac.service import invalidate_permission_cache

    invalidate_permission_cache()

    yield {
        "client": client,
        "me": uid,
        "boss": boss.id,
        "victim": victim.id,
        "delegate_role": delegate_role.id,
        "powerful": powerful.id,
        "audit_perm": _perm(db, "audit.view").id,
        "users_view_perm": _perm(db, "users.view").id,
    }

    db.session.rollback()
    ids = made_users + [uid]
    for rid in made_roles:
        db.session.execute(text("DELETE FROM user_roles WHERE role_id = :r"), {"r": rid})
        db.session.execute(text("DELETE FROM role_permissions WHERE role_id = :r"), {"r": rid})
    for u in made_users:
        db.session.execute(text("DELETE FROM user_roles WHERE user_id = :u"), {"u": u})
        db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :u"), {"u": u})
        db.session.execute(text("DELETE FROM users WHERE id = :u"), {"u": u})
    db.session.execute(text("DELETE FROM roles WHERE id = ANY(:ids)"), {"ids": made_roles})
    db.session.commit()
    del ids


def _edit_form(user, **over):
    data = {
        "full_name": user.full_name,
        "email": user.email,
        "is_active": "y",
        "avatar_url": "",
    }
    if user.is_superuser:
        data["is_superuser"] = "y"
    data.update(over)
    return data


def _reload(db, uid):
    db.session.expire_all()
    return db.session.get(User, uid)


# --- the superuser flag --------------------------------------------------


def test_delegate_cannot_make_themselves_superuser(db, world):
    me = _reload(db, world["me"])
    world["client"].post(
        f"/admin/users/{me.id}",
        data={**_edit_form(me), "is_superuser": "y", "role_ids": [world["delegate_role"]]},
    )
    assert _reload(db, world["me"]).is_superuser is False


def test_delegate_cannot_create_a_superuser(db, world):
    world["client"].post(
        "/admin/users/new",
        data={
            "username": "mintedroot",
            "full_name": "Minted Root",
            "email": "mintedroot@example.com",
            "password": "Strong-pass-12345",
            "is_active": "y",
            "is_superuser": "y",
        },
    )
    minted = User.query.filter_by(username="mintedroot").first()
    if minted is not None:  # created without the flag is acceptable; with it is not
        assert minted.is_superuser is False
        db.session.execute(text("DELETE FROM users WHERE id = :u"), {"u": minted.id})
        db.session.commit()


# --- roles carrying permissions the delegate lacks -------------------------


def test_delegate_cannot_give_themselves_a_more_powerful_role(db, world):
    me = _reload(db, world["me"])
    world["client"].post(
        f"/admin/users/{me.id}",
        data={**_edit_form(me), "role_ids": [world["delegate_role"], world["powerful"]]},
    )
    assert world["powerful"] not in {r.id for r in _reload(db, world["me"]).roles}


def test_delegate_cannot_add_a_permission_they_lack_to_a_role(db, world):
    world["client"].post(
        f"/admin/rbac/roles/{world['delegate_role']}/edit",
        data={
            "name": "delegate",
            "description": "t",
            "permission_ids": [world["users_view_perm"], world["audit_perm"]],
        },
    )
    db.session.expire_all()
    role = db.session.get(Role, world["delegate_role"])
    assert "audit.view" not in {p.code for p in role.permissions}


def test_delegate_cannot_create_a_role_with_permissions_they_lack(db, world):
    world["client"].post(
        "/admin/rbac/roles/new",
        data={"name": "sneaky", "description": "t", "permission_ids": [world["audit_perm"]]},
    )
    sneaky = Role.query.filter_by(name="sneaky").first()
    if sneaky is not None:
        codes = {p.code for p in sneaky.permissions}
        db.session.execute(
            text("DELETE FROM role_permissions WHERE role_id = :r"), {"r": sneaky.id}
        )
        db.session.execute(text("DELETE FROM roles WHERE id = :r"), {"r": sneaky.id})
        db.session.commit()
        assert "audit.view" not in codes


def test_delegate_cannot_delete_a_role_more_powerful_than_themselves(db, world):
    world["client"].post(f"/admin/rbac/roles/{world['powerful']}/delete")
    db.session.expire_all()
    assert db.session.get(Role, world["powerful"]).deleted_at is None


# --- superuser accounts ------------------------------------------------------


@pytest.mark.parametrize("action", ["lock", "delete"])
def test_delegate_cannot_lock_or_delete_a_superuser(db, world, action):
    world["client"].post(f"/admin/users/{world['boss']}/{action}")
    boss = _reload(db, world["boss"])
    assert boss.deleted_at is None
    assert not boss.is_locked


def test_delegate_cannot_demote_a_superuser(db, world):
    boss = _reload(db, world["boss"])
    form = _edit_form(boss)
    form.pop("is_superuser")
    world["client"].post(f"/admin/users/{boss.id}", data=form)
    assert _reload(db, world["boss"]).is_superuser is True


# --- what a delegate still may do --------------------------------------------


def test_delegate_can_still_manage_ordinary_users_within_their_permissions(db, world):
    victim = _reload(db, world["victim"])
    r = world["client"].post(
        f"/admin/users/{victim.id}",
        data={
            **_edit_form(victim, full_name="Renamed Victim"),
            "role_ids": [world["delegate_role"]],
        },
    )
    assert r.status_code == 302
    victim = _reload(db, world["victim"])
    assert victim.full_name == "Renamed Victim"
    assert world["delegate_role"] in {r.id for r in victim.roles}

    world["client"].post(f"/admin/users/{victim.id}/lock")
    assert _reload(db, world["victim"]).is_locked


def test_superuser_keeps_every_power(app, db, world):
    me = _reload(db, world["me"])
    me.is_superuser = True
    db.session.commit()
    victim = _reload(db, world["victim"])
    world["client"].post(
        f"/admin/users/{victim.id}",
        data={**_edit_form(victim), "is_superuser": "y", "role_ids": [world["powerful"]]},
    )
    victim = _reload(db, world["victim"])
    assert victim.is_superuser is True
    assert world["powerful"] in {r.id for r in victim.roles}


def _superuser_checkbox(html):
    import re

    return re.search(r"<input[^>]*name=\"is_superuser\"[^>]*>", html).group(0)


def test_superuser_checkbox_is_offered_only_to_superusers(db, world):
    client = world["client"]
    assert " disabled" in _superuser_checkbox(client.get("/admin/users/new").get_data(as_text=True))

    me = _reload(db, world["me"])
    me.is_superuser = True
    db.session.commit()
    assert " disabled" not in _superuser_checkbox(
        client.get("/admin/users/new").get_data(as_text=True)
    )
