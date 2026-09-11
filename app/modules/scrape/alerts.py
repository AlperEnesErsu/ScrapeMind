"""Saved searches and their alerts (Faz 7.1).

Runs a saved search, works out which matches the user has not been told about,
records the announcement and returns what to say.

The whole design turns on one thing, spelled out on `SavedSearchNotification`:
a paper can start matching long after it arrived, so "what is new" cannot be
answered with a timestamp. Everything here is built around answering "what have
we already said" instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from flask import current_app
from flask_babel import force_locale
from sqlalchemy.exc import IntegrityError

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import Paper, SavedSearch, SavedSearchNotification, UserPaper

logger = structlog.get_logger(__name__)

#: Most papers announced for one search in one run.
#:
#: A broad query matches hundreds a night, and a notification listing hundreds
#: is a notification nobody opens -- the feature gets switched off rather than
#: narrowed. Hitting this ceiling is itself the signal the query is too wide,
#: and `AlertResult.capped` exists so the interface can say so rather than
#: quietly truncating.
MAX_MATCHES_PER_RUN = 50

#: Filter keys a saved search may carry. Anything else in `filters` is ignored
#: rather than passed through: this dict is user-editable JSON reaching a
#: query builder, and forwarding unknown keys would turn a typo into a
#: TypeError at 03:00 in a worker.
ALLOWED_FILTERS = frozenset({"source", "quartile", "date_from", "date_to", "has_notes"})

VALID_CADENCES = ("daily", "weekly", "off")


@dataclass(frozen=True)
class AlertResult:
    """What one saved-search run found and announced."""

    search: SavedSearch
    papers: list[Paper]
    total_matched: int
    capped: bool

    @property
    def count(self) -> int:
        return len(self.papers)


def _filter_kwargs(search: SavedSearch) -> dict:
    raw = search.filters or {}
    return {k: v for k, v in raw.items() if k in ALLOWED_FILTERS and v not in (None, "")}


def find_new_matches(search: SavedSearch, *, limit: int | None = None) -> AlertResult:
    """Run one saved search and return the matches not yet announced.

    Announces nothing and writes nothing -- `announce` does that -- so this can
    be called from a preview endpoint without consuming the result.

    `limit` defaults to `MAX_MATCHES_PER_RUN` but reads it at call time rather
    than binding it as a default argument: a module constant used as a default
    is captured when the function is defined, which makes it impossible to
    override and quietly turns the constant into a literal.
    """
    from app.modules.scrape.service import search_user_papers_query

    if limit is None:
        limit = MAX_MATCHES_PER_RUN

    user = db.session.get(User, search.user_id)
    if user is None:
        return AlertResult(search=search, papers=[], total_matched=0, capped=False)

    query = search_user_papers_query(
        user,
        q=search.q,
        semantic=bool(search.semantic),
        **_filter_kwargs(search),
    )

    already = db.session.query(SavedSearchNotification.paper_id).filter(
        SavedSearchNotification.saved_search_id == search.id
    )
    query = query.filter(~UserPaper.paper_id.in_(already))

    # One extra row so "there were more than the cap" is a fact rather than an
    # inference from len(rows) == limit.
    rows = query.limit(limit + 1).all()
    capped = len(rows) > limit
    rows = rows[:limit]

    return AlertResult(
        search=search,
        papers=[link.paper for link in rows],
        total_matched=len(rows),
        capped=capped,
    )


def announce(result: AlertResult) -> int:
    """Record that these papers have been announced. Returns rows written.

    The unique constraint is the real guard: two runs overlapping -- a manual
    trigger while the nightly sweep is mid-flight -- must not announce the same
    paper twice, and application-level checking cannot promise that. A conflict
    means someone else got there first, which is success, not failure.
    """
    written = 0
    for paper in result.papers:
        row = SavedSearchNotification(saved_search_id=result.search.id, paper_id=paper.id)
        db.session.add(row)
        try:
            db.session.commit()
            written += 1
        except IntegrityError:
            db.session.rollback()
            logger.debug(
                "saved_search_already_announced",
                saved_search_id=result.search.id,
                paper_id=paper.id,
            )
    result.search.last_run_at = datetime.now(UTC)
    db.session.commit()
    return written


def _locale_for(user_id: int) -> str:
    """The recipient's language, falling back to the configured default."""
    from app.core.i18n.utils import SUPPORTED_LOCALES

    user = db.session.get(User, user_id)
    locale = getattr(user, "locale", None)
    if locale in SUPPORTED_LOCALES:
        return locale
    return current_app.config.get("BABEL_DEFAULT_LOCALE", "tr")


def notification_text(result: AlertResult) -> tuple[str, str]:
    """The title and body for one alert.

    Rolled up rather than one notification per paper: a query matching forty
    papers would otherwise produce forty rows in the bell menu and teach the
    user to ignore it.
    """
    from flask_babel import gettext as _

    title = _("New results for “%(name)s”", name=result.search.name)

    lines = [
        _("%(count)d new paper(s) match this saved search.", count=result.count),
        "",
    ]
    # A handful of titles makes the notification worth opening; the rest is
    # what the library link is for.
    for paper in result.papers[:5]:
        lines.append(f"• {paper.title}")
    if result.count > 5:
        lines.append(_("…and %(more)d more.", more=result.count - 5))
    if result.capped:
        lines.append("")
        lines.append(
            _(
                "This search matched more than %(cap)d papers, so only the first "
                "%(cap)d are listed. Narrowing it will make these alerts more useful.",
                cap=MAX_MATCHES_PER_RUN,
            )
        )

    return title, "\n".join(lines)


def run_saved_search(search: SavedSearch) -> AlertResult | None:
    """Find, announce and notify for one saved search.

    Returns None when there was nothing to say -- no notification is created
    for an empty run, the same "never call for nothing" contract the digest
    keeps.
    """
    from app.core.models.notification import add_notification

    if not search.is_active or search.cadence == "off":
        return None

    result = find_new_matches(search)
    if result.count == 0:
        search.last_run_at = datetime.now(UTC)
        db.session.commit()
        return None

    # Order matters, and it is the opposite of the obvious one.
    #
    # Marking papers announced and then building the notification means any
    # failure in between loses the alert *permanently and silently*: the rows
    # are committed, so those papers never match again, and the user is never
    # told. That is not hypothetical -- it is exactly what the locale bug below
    # did on its first run here, burning 150 papers for one saved search.
    #
    # Delivering first and marking afterwards trades that for a repeated alert
    # if the marking fails, which is visible, rare, and harmless. A duplicate
    # notification is a nuisance; a lost one is a feature that quietly does
    # nothing.
    #
    # `force_locale` is not decoration. Without it `_()` falls through to the
    # app's locale selector, which reads `request.args` -- and there is no
    # request here, this runs on the beat schedule. Every alert raised
    # RuntimeError inside `alerts.run_for_user`, was swallowed by the
    # per-search `except Exception` that exists so one bad search does not
    # lose the others, and was logged as `alerts_search_failed`. The feature
    # was dead end to end and looked, from the outside, like "no new matches".
    #
    # It also has to be the *recipient's* language: this text is read in the
    # bell menu by whoever saved the search, not by whoever is running the
    # worker. `digest_tasks` and `report_tasks` both do exactly this; the
    # saved-search path was written without them in view.
    locale = _locale_for(search.user_id)
    with force_locale(locale):
        title, message = notification_text(result)
    add_notification(search.user_id, title, message)
    announce(result)

    logger.info(
        "saved_search_alert",
        saved_search_id=search.id,
        user_id=search.user_id,
        count=result.count,
        capped=result.capped,
    )
    return result
