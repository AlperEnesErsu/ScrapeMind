"""Author groups (Faz 6): named sets of followed authors gathered for an
on-demand "author_group" report, as opposed to `UserAuthor.active` which
feeds the nightly ingest.

The load-bearing behaviour is the `active` split: a brand-new `UserAuthor`
row created only to join a group must not start pushing that author's new
papers into the nightly feed, but a group must never be able to touch an
author's follow state indirectly, and no function here may cross a user
ownership boundary.
"""

from __future__ import annotations

import pytest
import requests
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.modules.scrape import service
from app.modules.scrape.models import AuthorGroup, AuthorGroupMember, UserAuthor
from app.modules.scrape.sources import openalex_source as oa

_ORCID = "0000-0002-1825-0097"
_AUTHOR_ID = "A5023888391"


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def _author_json(name="Jane Smith", author_id=_AUTHOR_ID, orcid=_ORCID):
    return {
        "id": f"https://openalex.org/{author_id}",
        "display_name": name,
        "orcid": f"https://orcid.org/{orcid}" if orcid else None,
        "works_count": 42,
    }


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(oa, "openalex_slot", lambda: True)


def _make_user(db, suffix):
    user = User.query.filter_by(email=f"authorgroup{suffix}@example.test").first()
    if user is None:
        user = User(
            username=f"authorgroup{suffix}",
            email=f"authorgroup{suffix}@example.test",
            full_name=f"Author Group User {suffix}",
            password_hash=generate_password_hash("password123"),
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
    return user


def _cleanup_user(db, uid):
    db.session.rollback()
    db.session.execute(
        text(
            "DELETE FROM author_group_members WHERE group_id IN "
            "(SELECT id FROM author_groups WHERE user_id = :uid)"
        ),
        {"uid": uid},
    )
    db.session.execute(text("DELETE FROM author_groups WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM reports WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM user_authors WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


@pytest.fixture
def a_user(db):
    user = _make_user(db, "a")
    uid = user.id
    yield user
    _cleanup_user(db, uid)


@pytest.fixture
def other_user(db):
    user = _make_user(db, "b")
    uid = user.id
    yield user
    _cleanup_user(db, uid)


def _follow(user, monkeypatch, *, author_id=_AUTHOR_ID, name="Jane Smith", activate=True):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json(name, author_id)))
    row, error = service.follow_author(user, author_id, activate=activate)
    assert error is None, error
    return row


# ----------------------------------------------------------------------------
# Group CRUD — happy path
# ----------------------------------------------------------------------------


def test_create_list_rename_delete_group(app, db, a_user):
    row, error = service.create_author_group(a_user, "Immunology watch", "key people")
    assert error is None
    assert row.name == "Immunology watch"
    assert row.description == "key people"

    assert [g.id for g in service.list_author_groups(a_user)] == [row.id]

    renamed, error = service.rename_author_group(a_user, row.id, "Renamed")
    assert error is None
    assert renamed.name == "Renamed"

    assert service.delete_author_group(a_user, row.id) is True
    assert service.list_author_groups(a_user) == []


def test_create_group_rejects_blank_name(app, db, a_user):
    row, error = service.create_author_group(a_user, "   ")
    assert row is None and error


def test_renaming_to_its_own_name_is_a_noop_success(app, db, a_user):
    row, _ = service.create_author_group(a_user, "Group A")
    again, error = service.rename_author_group(a_user, row.id, "Group A")
    assert error is None
    assert again.id == row.id


def test_delete_of_unknown_group_returns_false(app, db, a_user):
    assert service.delete_author_group(a_user, 999_999) is False


# ----------------------------------------------------------------------------
# Caps
# ----------------------------------------------------------------------------


def test_group_cap_is_enforced(app, db, a_user, monkeypatch):
    monkeypatch.setattr(service, "MAX_AUTHOR_GROUPS", 1)
    row, error = service.create_author_group(a_user, "First")
    assert error is None

    row2, error = service.create_author_group(a_user, "Second")
    assert row2 is None
    assert str(error) == str(service.GROUP_CAP_MESSAGE)


def test_same_name_second_group_is_rejected(app, db, a_user):
    service.create_author_group(a_user, "Dup")
    row, error = service.create_author_group(a_user, "Dup")
    assert row is None
    assert str(error) == str(service.GROUP_NAME_TAKEN_MESSAGE)


def test_rename_to_an_existing_name_is_rejected(app, db, a_user):
    service.create_author_group(a_user, "One")
    two, _ = service.create_author_group(a_user, "Two")
    renamed, error = service.rename_author_group(a_user, two.id, "One")
    assert renamed is None
    assert str(error) == str(service.GROUP_NAME_TAKEN_MESSAGE)


def test_member_cap_is_enforced(app, db, a_user, monkeypatch):
    monkeypatch.setattr(service, "MAX_GROUP_MEMBERS", 1)
    group, _ = service.create_author_group(a_user, "Small group")
    author1 = _follow(a_user, monkeypatch, author_id="A1", name="First")
    author2 = _follow(a_user, monkeypatch, author_id="A2", name="Second")

    member, error = service.add_group_member(a_user, group.id, author1.id)
    assert error is None

    member2, error = service.add_group_member(a_user, group.id, author2.id)
    assert member2 is None
    assert str(error) == str(service.MEMBER_CAP_MESSAGE)


# ----------------------------------------------------------------------------
# Ownership boundaries
# ----------------------------------------------------------------------------


def test_other_users_group_is_invisible_to_list(app, db, a_user, other_user):
    service.create_author_group(a_user, "Mine")
    assert service.list_author_groups(other_user) == []


def test_rename_of_another_users_group_fails(app, db, a_user, other_user):
    group, _ = service.create_author_group(a_user, "Mine")
    renamed, error = service.rename_author_group(other_user, group.id, "Stolen")
    assert renamed is None
    assert str(error) == str(service.GROUP_NOT_FOUND_MESSAGE)
    assert AuthorGroup.query.get(group.id).name == "Mine"


def test_delete_of_another_users_group_fails(app, db, a_user, other_user):
    group, _ = service.create_author_group(a_user, "Mine")
    assert service.delete_author_group(other_user, group.id) is False
    assert AuthorGroup.query.get(group.id) is not None


def test_add_member_to_another_users_group_fails(app, db, a_user, other_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    author = _follow(other_user, monkeypatch, author_id="A1", name="Someone")

    member, error = service.add_group_member(other_user, group.id, author.id)
    assert member is None
    assert str(error) == str(service.GROUP_NOT_FOUND_MESSAGE)


def test_add_another_users_author_to_own_group_fails(app, db, a_user, other_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    other_author = _follow(other_user, monkeypatch, author_id="A1", name="Someone")

    member, error = service.add_group_member(a_user, group.id, other_author.id)
    assert member is None
    assert str(error) == str(service.GROUP_AUTHOR_NOT_FOUND_MESSAGE)


def test_remove_member_scoped_to_owned_group(app, db, a_user, other_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    author = _follow(a_user, monkeypatch, author_id="A1", name="Someone")
    member, _ = service.add_group_member(a_user, group.id, author.id)

    assert service.remove_group_member(other_user, group.id, member.id) is False
    assert AuthorGroupMember.query.get(member.id) is not None

    assert service.remove_group_member(a_user, group.id, member.id) is True
    assert AuthorGroupMember.query.get(member.id) is None


# ----------------------------------------------------------------------------
# Membership de-duplication
# ----------------------------------------------------------------------------


def test_adding_the_same_member_twice_is_deduplicated(app, db, a_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    author = _follow(a_user, monkeypatch, author_id="A1", name="Someone")

    first, error = service.add_group_member(a_user, group.id, author.id)
    assert error is None
    second, error = service.add_group_member(a_user, group.id, author.id)
    assert error is None
    assert second.id == first.id
    assert service.count_group_members(group) == 1


# ----------------------------------------------------------------------------
# The active-flag split — the load-bearing behaviour
# ----------------------------------------------------------------------------


def test_new_author_resolved_for_a_group_starts_inactive(app, db, a_user, monkeypatch):
    """Route flow: follow_author(activate=False) then add_group_member — a
    brand-new UserAuthor row created only for a group must not feed the
    nightly ingest."""
    group, _ = service.create_author_group(a_user, "Mine")
    author = _follow(a_user, monkeypatch, author_id="A1", name="Someone", activate=False)
    assert author.active is False

    member, error = service.add_group_member(a_user, group.id, author.id)
    assert error is None
    assert author.active is False  # add_group_member must not touch it either


def test_already_active_author_stays_active_when_grouped(app, db, a_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    author = _follow(a_user, monkeypatch, author_id="A1", name="Someone", activate=True)
    assert author.active is True

    service.add_group_member(a_user, group.id, author.id)
    assert author.active is True


def test_activate_false_does_not_pause_an_already_followed_author(app, db, a_user, monkeypatch):
    """`activate=False` only affects a brand-new row — re-resolving an author
    who is already actively followed must not demote them."""
    author = _follow(a_user, monkeypatch, author_id="A1", name="Someone", activate=True)
    assert author.active is True

    again = _follow(a_user, monkeypatch, author_id="A1", name="Someone", activate=False)
    assert again.id == author.id
    assert again.active is True


# ----------------------------------------------------------------------------
# Deleting a group leaves author-following untouched
# ----------------------------------------------------------------------------


def test_deleting_a_group_drops_membership_but_keeps_the_author(app, db, a_user, monkeypatch):
    group, _ = service.create_author_group(a_user, "Mine")
    author = _follow(a_user, monkeypatch, author_id="A1", name="Someone")
    member, _ = service.add_group_member(a_user, group.id, author.id)
    member_id = member.id
    author_id = author.id

    assert service.delete_author_group(a_user, group.id) is True

    assert AuthorGroupMember.query.get(member_id) is None
    assert UserAuthor.query.get(author_id) is not None


# ----------------------------------------------------------------------------
# fetch_author enrichment reaching follow_author
# ----------------------------------------------------------------------------


def test_follow_author_stores_institution_and_cited_by_count(app, db, a_user, monkeypatch):
    payload = _author_json()
    payload["last_known_institutions"] = [{"display_name": "Example University"}]
    payload["cited_by_count"] = 999
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(payload))

    row, error = service.follow_author(a_user, _AUTHOR_ID)
    assert error is None
    assert row.institution == "Example University"
    assert row.cited_by_count == 999
    assert row.works_count == 42


def test_refollowing_refreshes_the_openalex_snapshot(app, db, a_user, monkeypatch):
    """Unlike a fill-only field, these counts change over time and must be
    overwritten by a fresher resolution, not just filled in when empty."""
    payload = _author_json()
    payload["last_known_institutions"] = [{"display_name": "Old University"}]
    payload["cited_by_count"] = 10
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(payload))
    row, _ = service.follow_author(a_user, _AUTHOR_ID)
    assert row.institution == "Old University"
    assert row.cited_by_count == 10

    payload2 = _author_json()
    payload2["last_known_institutions"] = [{"display_name": "New University"}]
    payload2["cited_by_count"] = 500
    payload2["works_count"] = 77
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(payload2))
    again, _ = service.follow_author(a_user, _AUTHOR_ID)

    assert again.id == row.id
    assert again.institution == "New University"
    assert again.cited_by_count == 500
    assert again.works_count == 77


def test_group_flow_does_not_resurrect_a_paused_author(app, db, a_user, monkeypatch):
    """A deliberate pause survives being named in a group.

    `active` means "push this author's new work into my nightly feed".
    Pausing is a user saying "stop doing that". Adding the same author to a
    group later is about a report, not about the feed — so the group path
    (`activate=False`) must not quietly undo the pause. Without this guard
    the re-follow branch flipped `active` back to True for everyone.
    """
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    row, err = service.follow_author(a_user, _ORCID)
    assert err is None and row.active is True

    service.toggle_user_author(a_user, row.id)
    db.session.refresh(row)
    assert row.active is False, "kurulum: yazar duraklatilmis olmali"

    group, _ = service.create_author_group(a_user, "Komite")
    again, err = service.follow_author(a_user, _ORCID, activate=False)
    assert err is None
    member, err = service.add_group_member(a_user, group.id, again.id)
    assert err is None and member is not None

    db.session.refresh(row)
    assert row.active is False, "gruba eklemek duraklatmayi bozmamali"


def test_plain_refollow_still_resumes_a_paused_author(app, db, a_user, monkeypatch):
    """The counterpart: on the normal follow path a re-follow *is* the user
    asking for that author back, so the pause is lifted as before."""
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    row, _ = service.follow_author(a_user, _ORCID)
    service.toggle_user_author(a_user, row.id)
    db.session.refresh(row)
    assert row.active is False

    service.follow_author(a_user, _ORCID)
    db.session.refresh(row)
    assert row.active is True
