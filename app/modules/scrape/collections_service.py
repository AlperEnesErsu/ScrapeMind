"""Collections — user-named folders of papers.

Every function is user-scoped: a collection and the papers put into it must
belong to the acting user. Ownership is enforced here (the get_* helpers
return None on a mismatch) so routes stay thin and can treat miss ==
forbidden == 404.
"""

from __future__ import annotations

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import Collection, Paper, UserPaper, collection_papers


def list_collections(user: User) -> list[Collection]:
    return (
        Collection.query.filter_by(user_id=user.id, deleted_at=None).order_by(Collection.name).all()
    )


def get_collection(user: User, collection_id: int) -> Collection | None:
    """A collection owned by this user, or None (miss or someone else's)."""
    return Collection.query.filter_by(id=collection_id, user_id=user.id, deleted_at=None).first()


def list_papers_for_display(coll: Collection) -> list[UserPaper]:
    """`coll.papers`, but with `Paper.video_summary` eager-loaded for
    scrape/_paper_card.html — the plain `Collection.papers` relationship
    (lazy="select") would otherwise fire one extra SELECT per row the
    template renders (N+1) checking `r.paper.video_summary`. Collections are
    user-curated folders, so this is a re-query rather than reusing the
    relationship's own lazy load, but it's still a single extra query, not
    one per paper."""
    from sqlalchemy.orm import joinedload

    return (
        UserPaper.query.join(collection_papers, collection_papers.c.user_paper_id == UserPaper.id)
        .filter(collection_papers.c.collection_id == coll.id)
        .options(joinedload(UserPaper.paper).joinedload(Paper.video_summary))
        .order_by(db.desc(UserPaper.created_at))
        .all()
    )


def create_collection(user: User, name: str, description: str | None = None) -> tuple:
    """Create a collection. Returns (collection, error). Names are unique per
    user; a duplicate is a validation error, not a crash."""
    name = (name or "").strip()
    if not name:
        return None, "Collection name is required."
    if len(name) > 120:
        return None, "Collection name is too long."
    existing = Collection.query.filter_by(user_id=user.id, name=name, deleted_at=None).first()
    if existing is not None:
        return None, "A collection with that name already exists."
    coll = Collection(user_id=user.id, name=name, description=(description or "").strip() or None)
    db.session.add(coll)
    db.session.commit()
    return coll, None


def rename_collection(coll: Collection, name: str, description: str | None) -> tuple:
    name = (name or "").strip()
    if not name:
        return False, "Collection name is required."
    clash = Collection.query.filter(
        Collection.user_id == coll.user_id,
        Collection.name == name,
        Collection.id != coll.id,
        Collection.deleted_at.is_(None),
    ).first()
    if clash is not None:
        return False, "A collection with that name already exists."
    coll.name = name
    coll.description = (description or "").strip() or None
    db.session.commit()
    return True, None


def delete_collection(coll: Collection) -> None:
    """Soft-delete the collection. The junction rows are left to the ORM's
    cascade on the FK (ON DELETE CASCADE) only on hard delete, so we clear
    membership explicitly here to keep a soft-deleted folder empty."""
    coll.papers = []
    coll.soft_delete()
    db.session.commit()


def _own_user_paper(user: User, user_paper_id: int) -> UserPaper | None:
    return UserPaper.query.filter_by(id=user_paper_id, user_id=user.id).first()


def add_paper(coll: Collection, user: User, user_paper_id: int) -> tuple[bool, str | None]:
    """Add one of the user's papers to a collection. Idempotent — adding a
    paper already in the collection is a no-op success. Rejects a paper the
    user doesn't own (404-worthy) so a collection can't reference a stranger's
    row."""
    up = _own_user_paper(user, user_paper_id)
    if up is None:
        return False, "not_found"
    if up not in coll.papers:
        coll.papers.append(up)
        db.session.commit()
    return True, None


def remove_paper(coll: Collection, user_paper_id: int) -> None:
    """Remove a paper from a collection. Silent if it wasn't a member."""
    up = next((p for p in coll.papers if p.id == user_paper_id), None)
    if up is not None:
        coll.papers.remove(up)
        db.session.commit()


def collection_ids_for_paper(user: User, user_paper_id: int) -> set[int]:
    """Which of the user's collections already contain this paper — used to
    pre-check the boxes in the 'add to collection' menu."""
    coll = (
        db.session.query(Collection.id)
        .join(Collection.papers)
        .filter(Collection.user_id == user.id, UserPaper.id == user_paper_id)
        .all()
    )
    return {c for (c,) in coll}
