"""Author following."""

from __future__ import annotations

import structlog
from flask_babel import lazy_gettext as _l

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import (
    UserAuthor,
)
from app.modules.scrape.service.papers import link_user_paper, upsert_paper

logger = structlog.get_logger()


# Author following (Faz 5.4)

#: Per-user cap on followed authors. Originally sized as a politeness budget
#: against a free API — one OpenAlex request per *active* follow per night —
#: hence a module constant rather than an admin setting. That reasoning no
#: longer covers the whole count: `count_user_authors` does not filter on
#: `active`, so a `UserAuthor` row created only to sit in an `AuthorGroup`
#: (see Faz 6's group membership, which deliberately opens new rows
#: `active=False` so grouping does not feed the nightly ingest) still spends
#: a slot even though it costs zero nightly requests. The cap stays a single
#: number anyway: splitting it into separate active/passive budgets would
#: let a user accumulate an unbounded number of `UserAuthor` rows overall,
#: which is its own (row-count, UI-list-length) cost independent of the
#: OpenAlex request budget.
MAX_USER_AUTHORS = 50


AUTHOR_CAP_MESSAGE = _l("Author limit reached. Unfollow one before adding another.")


def list_user_authors(user: User) -> list[UserAuthor]:
    return UserAuthor.query.filter_by(user_id=user.id).order_by(UserAuthor.author_name.asc()).all()


def count_user_authors(user: User) -> int:
    return UserAuthor.query.filter_by(user_id=user.id).count()


def follow_author(
    user: User, raw: str, *, activate: bool = True
) -> tuple[UserAuthor | None, str | None]:
    """Follow an author by ORCID or OpenAlex id. Returns `(row, None)` or
    `(None, error_message)` — never raises for bad user input.

    Resolution happens here, once, rather than per nightly run: it is the
    expensive and failure-prone half, and doing it on follow means a typo
    surfaces immediately in front of the user instead of disappearing into a
    task log a night later.

    Re-following an author already on the list reactivates a paused row rather
    than creating a duplicate, and consumes no cap slot — same shape as
    `add_user_channel`.

    `institution`/`works_count`/`cited_by_count` are refreshed on every call,
    including the re-follow-of-an-existing-row path — the same
    always-refresh treatment `Paper.cited_by_count` gets, and for the same
    reason (see `UserAuthor`'s docstring): these are an OpenAlex snapshot
    "as of the last resolution", never a historical value worth protecting
    from being overwritten by a fresher one.

    `activate` controls only the initial `active` value of a *brand-new* row.
    It exists for the author-group flow (Faz 6): adding someone to an
    `AuthorGroup` who is not followed at all yet means the route resolves
    them here first, with `activate=False`, then calls `add_group_member` —
    grouping is "gather this author's work into a report on demand", not
    "push their new papers into my nightly feed" (see `AuthorGroup`'s
    docstring), so a row that exists only because of that ask must not start
    feeding the nightly ingest. An *existing* row is unaffected by
    `activate=False` either way — it still reactivates if paused, same as
    before: this parameter is not a way to pause an author who is already
    being followed.
    """
    from app.modules.scrape.sources.openalex_source import fetch_author

    raw = (raw or "").strip()
    if not raw:
        return None, _l("Please enter an ORCID or OpenAlex author id.")

    try:
        resolved = fetch_author(raw)
    except Exception:  # noqa: BLE001 — a lookup failure is user-facing, not a 500
        logger.exception("author_resolve_failed", user_id=user.id)
        return None, _l("Could not reach OpenAlex right now. Please try again.")

    if resolved is None:
        return None, _l("No author found for that ORCID or OpenAlex id.")

    existing = UserAuthor.query.filter_by(user_id=user.id, openalex_id=resolved["id"]).first()
    if existing is not None:
        # Re-following someone you had paused means "put them back in my
        # feed" — but only on the follow path. `activate=False` is the group
        # path, where the author is being named for a report; that must not
        # silently undo a deliberate pause and start pushing their papers
        # into the nightly feed again.
        if activate and not existing.active:
            existing.active = True
        existing.institution = resolved.get("institution")
        existing.works_count = resolved.get("works_count")
        existing.cited_by_count = resolved.get("cited_by_count")
        db.session.commit()
        return existing, None

    if count_user_authors(user) >= MAX_USER_AUTHORS:
        logger.info("user_author_cap_reached", user_id=user.id, cap=MAX_USER_AUTHORS)
        return None, AUTHOR_CAP_MESSAGE

    name = resolved["name"] or raw
    # The table's unique constraint is on (user_id, author_name) and predates
    # openalex_id. Two different OpenAlex authors can share a display name, so
    # disambiguate rather than letting the insert fail.
    if UserAuthor.query.filter_by(user_id=user.id, author_name=name).first() is not None:
        name = f"{name} ({resolved['id']})"

    row = UserAuthor(
        user_id=user.id,
        author_name=name[:128],
        openalex_id=resolved["id"],
        orcid=resolved["orcid"],
        active=activate,
        institution=resolved.get("institution"),
        works_count=resolved.get("works_count"),
        cited_by_count=resolved.get("cited_by_count"),
    )
    db.session.add(row)
    db.session.commit()
    logger.info("author_followed", user_id=user.id, openalex_id=resolved["id"])
    return row, None


def unfollow_author(user: User, author_id: int) -> bool:
    """Delete a follow. Returns whether a row was actually removed."""
    row = UserAuthor.query.filter_by(id=author_id, user_id=user.id).first()
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def toggle_user_author(user: User, author_id: int) -> UserAuthor | None:
    """Pause/resume a follow, keeping the row and its `last_work_at`
    high-water mark — deleting and re-adding would lose the latter and
    re-import the author's back catalogue."""
    row = UserAuthor.query.filter_by(id=author_id, user_id=user.id).first()
    if row is None:
        return None
    row.active = not row.active
    db.session.commit()
    return row


def ingest_user_authors(user: User, *, max_results: int = 25) -> dict:
    """Fetch new works for every author this user follows.

    Returns the same summary shape as the other scan paths (`{"sources": {…},
    "hits": n, "linked": n}`) so `apply_scan_result` reads it unchanged, with
    `-1` marking an author whose lookup failed.

    Each author is asked only for works published since `last_work_at`, which
    is advanced afterwards. A follow with no `openalex_id` (resolution failed
    at some point) is skipped rather than retried here — re-resolving belongs
    in the follow path, where a human can see the error.
    """
    from app.modules.scrape.sources.openalex_source import works_by_author

    authors = [a for a in list_user_authors(user) if a.active and a.openalex_id]
    if not authors:
        return {"hits": 0, "linked": 0, "reason": "no_authors"}

    hits = 0
    linked = 0
    summary: dict[str, int] = {}
    for author in authors:
        try:
            payloads = works_by_author(
                author.openalex_id, since=author.last_work_at, max_results=max_results
            )
        except Exception:  # noqa: BLE001 — one bad author must not kill the run
            logger.exception(
                "author_ingest_failed", user_id=user.id, openalex_id=author.openalex_id
            )
            summary[author.author_name] = -1
            continue

        summary[author.author_name] = len(payloads)
        hits += len(payloads)
        newest = author.last_work_at
        for payload in payloads:
            paper = upsert_paper(payload)
            _, created = link_user_paper(user, paper, matched_keyword=author.author_name[:64])
            if created:
                linked += 1
            if payload.published_at and (newest is None or payload.published_at > newest):
                newest = payload.published_at
        author.last_work_at = newest

    db.session.commit()
    logger.info("author_ingest_done", user_id=user.id, hits=hits, linked=linked)
    return {"hits": hits, "linked": linked, "sources": summary}
