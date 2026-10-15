"""Author groups: named sets of followed authors."""

from __future__ import annotations

import structlog
from flask_babel import lazy_gettext as _l

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import (
    AuthorGroup,
    AuthorGroupMember,
    UserAuthor,
)

logger = structlog.get_logger()


# Author groups (Faz 6) — named sets of followed authors for on-demand reports

#: Per-user cap on author groups. A small, human-curated number — nobody
#: sanely maintains more than a handful of report groupings — kept as a
#: module constant like `MAX_USER_AUTHORS`/`MAX_USER_FEEDS` rather than an
#: admin setting.
MAX_AUTHOR_GROUPS = 10


#: Per-group cap on members. Bounds how much a single "author_group" `Report`
#: run has to fetch and render — not a politeness budget against an API the
#: way `MAX_USER_AUTHORS` is, since group membership itself makes no request.
MAX_GROUP_MEMBERS = 20


GROUP_CAP_MESSAGE = _l("Group limit reached. Remove one before adding another.")


MEMBER_CAP_MESSAGE = _l("Group is full. Remove a member before adding another.")


GROUP_NAME_TAKEN_MESSAGE = _l("You already have a group with that name.")


GROUP_NAME_REQUIRED_MESSAGE = _l("Please enter a group name.")


GROUP_NOT_FOUND_MESSAGE = _l("Group not found.")


GROUP_AUTHOR_NOT_FOUND_MESSAGE = _l("Author not found.")


def list_author_groups(user: User) -> list[AuthorGroup]:
    return AuthorGroup.query.filter_by(user_id=user.id).order_by(AuthorGroup.name.asc()).all()


def count_author_groups(user: User) -> int:
    return AuthorGroup.query.filter_by(user_id=user.id).count()


def _owned_author_group(user: User, group_id: int) -> AuthorGroup | None:
    """Fetch a group only if it belongs to `user` — the ownership boundary
    every group-mutating function below goes through, same pattern as
    `unfollow_author`/`toggle_user_author` scoping their lookup to
    `user_id=user.id` instead of trusting a bare `group_id`."""
    return AuthorGroup.query.filter_by(id=group_id, user_id=user.id).first()


def create_author_group(
    user: User, name: str, description: str | None = None
) -> tuple[AuthorGroup | None, str | None]:
    """Create a named group. Returns `(row, None)` or `(None, error_message)`
    — never raises for bad user input, same contract as `follow_author`."""
    name = (name or "").strip()
    if not name:
        return None, GROUP_NAME_REQUIRED_MESSAGE

    if count_author_groups(user) >= MAX_AUTHOR_GROUPS:
        logger.info("author_group_cap_reached", user_id=user.id, cap=MAX_AUTHOR_GROUPS)
        return None, GROUP_CAP_MESSAGE

    if AuthorGroup.query.filter_by(user_id=user.id, name=name[:120]).first() is not None:
        return None, GROUP_NAME_TAKEN_MESSAGE

    description = (description or "").strip() or None
    row = AuthorGroup(user_id=user.id, name=name[:120], description=description)
    db.session.add(row)
    db.session.commit()
    logger.info("author_group_created", user_id=user.id, group_id=row.id)
    return row, None


def rename_author_group(
    user: User, group_id: int, name: str
) -> tuple[AuthorGroup | None, str | None]:
    """Rename a group the user owns. Renaming to the group's own current name
    is a no-op success, not a false "name taken"."""
    group = _owned_author_group(user, group_id)
    if group is None:
        return None, GROUP_NOT_FOUND_MESSAGE

    name = (name or "").strip()
    if not name:
        return None, GROUP_NAME_REQUIRED_MESSAGE
    name = name[:120]

    clash = AuthorGroup.query.filter_by(user_id=user.id, name=name).first()
    if clash is not None and clash.id != group.id:
        return None, GROUP_NAME_TAKEN_MESSAGE

    group.name = name
    db.session.commit()
    return group, None


def delete_author_group(user: User, group_id: int) -> bool:
    """Delete a group the user owns. Its `AuthorGroupMember` rows cascade
    (`AuthorGroup.members` is `cascade="all, delete-orphan"`); the
    `UserAuthor` rows themselves are untouched — author-following is a
    separate concept from any one group's membership, see `AuthorGroup`'s
    docstring."""
    group = _owned_author_group(user, group_id)
    if group is None:
        return False
    db.session.delete(group)
    db.session.commit()
    return True


def list_group_members(group: AuthorGroup) -> list[UserAuthor]:
    """The `UserAuthor` rows in `group`. Takes the group row itself rather
    than `(user, group_id)` — the caller already owns-checked it via
    `list_author_groups`/`_owned_author_group` to get it, so this is a plain
    read, not another ownership boundary."""
    return (
        UserAuthor.query.join(AuthorGroupMember, AuthorGroupMember.user_author_id == UserAuthor.id)
        .filter(AuthorGroupMember.group_id == group.id)
        .order_by(UserAuthor.author_name.asc())
        .all()
    )


def count_group_members(group: AuthorGroup) -> int:
    return group.members.count()


def add_group_member(
    user: User, group_id: int, user_author_id: int
) -> tuple[AuthorGroupMember | None, str | None]:
    """Add an already-followed author to a group the user owns. Returns
    `(row, None)` or `(None, error_message)` — never raises.

    Both `group_id` and `user_author_id` are ownership-checked against
    `user`, independently — a group id and an author id from two different
    users must not be combinable into a membership row.

    Deliberately does **not** create the `UserAuthor` row itself and does
    **not** touch its `active` flag: this function only ever receives the id
    of a row that already exists. The "new author, added only for a group"
    case (see `AuthorGroup`'s docstring: grouping is not "push into my nightly
    feed") is handled one layer up, by the caller resolving the author first
    through `follow_author(user, raw, activate=False)` — which is what
    starts that row `active=False` — and *then* calling this function with
    the id it returns. An author who was already being followed (`active`
    True or False) keeps whatever `active` value it already had; being added
    to a group is not a reason to change it either way.

    Adding the same author twice is idempotent: the existing membership row
    is returned rather than raising a unique-constraint error or silently
    duplicating.
    """
    group = _owned_author_group(user, group_id)
    if group is None:
        return None, GROUP_NOT_FOUND_MESSAGE

    author = UserAuthor.query.filter_by(id=user_author_id, user_id=user.id).first()
    if author is None:
        return None, GROUP_AUTHOR_NOT_FOUND_MESSAGE

    existing = AuthorGroupMember.query.filter_by(
        group_id=group.id, user_author_id=author.id
    ).first()
    if existing is not None:
        return existing, None

    if count_group_members(group) >= MAX_GROUP_MEMBERS:
        logger.info(
            "author_group_member_cap_reached",
            user_id=user.id,
            group_id=group.id,
            cap=MAX_GROUP_MEMBERS,
        )
        return None, MEMBER_CAP_MESSAGE

    member = AuthorGroupMember(group_id=group.id, user_author_id=author.id)
    db.session.add(member)
    db.session.commit()
    logger.info(
        "author_group_member_added", user_id=user.id, group_id=group.id, user_author_id=author.id
    )
    return member, None


def remove_group_member(user: User, group_id: int, member_id: int) -> bool:
    """Remove one membership row. `member_id` is the `AuthorGroupMember.id`,
    not the `UserAuthor.id` — the group scoping in the query is the
    ownership check, since a group not owned by `user` never matches."""
    group = _owned_author_group(user, group_id)
    if group is None:
        return False
    member = AuthorGroupMember.query.filter_by(id=member_id, group_id=group.id).first()
    if member is None:
        return False
    db.session.delete(member)
    db.session.commit()
    return True
