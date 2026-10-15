"""Per-user source selection (opt-out; no row == enabled)."""

from __future__ import annotations

import structlog

from app.core.models.user import User
from app.extensions import db
from app.modules.scrape.models import (
    UserSource,
)
from app.modules.scrape.sources import (
    SOURCE_META,
    admin_optin_key,
    enabled_sources,
)

logger = structlog.get_logger()


# Per-user source selection (opt-out; no row == enabled)


def admin_opted_in(setting_key: str) -> bool:
    """Whether an admin has switched on the `requires_admin_optin` gate named
    by `setting_key` (Faz 5.1).

    Defaults to False — a licensed or metered source stays off until someone
    with `system.manage` deliberately turns it on. A failed read is False for
    the same reason `ratelimit.consume_quota` is fail-closed: when the answer
    is "we can't tell", not calling a paid API is the safe direction.
    """
    from app.core.settings.service import get_system_setting

    try:
        return bool(get_system_setting(setting_key, False))
    except Exception:  # noqa: BLE001 — see the docstring: fail closed
        logger.warning("admin_optin_read_failed", setting_key=setting_key)
        return False


def list_user_source_prefs(user: User) -> dict[str, bool]:
    """Map of source_name -> enabled for this user's explicit choices only.
    Sources without a row are absent here (they default to enabled)."""
    rows = UserSource.query.filter_by(user_id=user.id).all()
    return {r.source_name: r.enabled for r in rows}


def effective_source_prefs(user: User, *, user_topics: list[str] | None = None) -> dict[str, bool]:
    """Effective enabled/disabled state for every deployment-enabled source,
    folding the interest-aware default (Faz 3 Bölüm C) in under any explicit
    `UserSource` override.

    Resolution order per source:
      0. `requires_admin_optin` and the admin has not opted in — off, and the
         explicit row in step 1 does not get to override it (Faz 5.1). This
         gate is a deployment-level licensing/cost decision, so it outranks
         user preference; a stale `UserSource` row from before the admin
         turned the source off must not keep scraping it.
      1. Explicit `UserSource` row — always wins, whichever way it's set.
      2. No row + `category == "academic"` / `"patent"` — on (unchanged Faz 1/2
         behaviour; these databases are relevant regardless of specialty)…
         except for a source still carrying an admin opt-in, which is
         **default off** even once opted into: someone paying per request
         should turn it on deliberately, not discover it in a bill.
      3. No row + `category == "feed"` — on only if the source's `topics`
         intersect `classify_user_topics(user)` (Faz 3's actual point: a
         history researcher doesn't get AI industry feeds default-on).

    `user_topics` can be passed in by a caller that already classified this
    user this request (e.g. the sources-card context builder) to skip a
    redundant (if cheap/cached) lookup; otherwise it's resolved lazily, and
    only when step 3 is actually reached (i.e. the user has at least one
    un-overridden feed source).
    """
    prefs = list_user_source_prefs(user)
    out: dict[str, bool] = {}
    suggested: set[str] | None = None
    # One SystemSettings read per distinct gate, not per gated source.
    optin_cache: dict[str, bool] = {}
    for name in enabled_sources():
        optin_key = admin_optin_key(name)
        if optin_key is not None:
            if optin_key not in optin_cache:
                optin_cache[optin_key] = admin_opted_in(optin_key)
            if not optin_cache[optin_key]:
                out[name] = False
                continue
        if name in prefs:
            out[name] = prefs[name]
            continue
        if optin_key is not None:
            out[name] = False
            continue
        meta = SOURCE_META.get(name, {})
        if meta.get("category") != "feed":
            out[name] = True
            continue
        if suggested is None:
            from app.modules.scrape.ai_service import classify_user_topics
            from app.modules.scrape.sources import suggested_sources

            if user_topics is None:
                user_topics = classify_user_topics(user)
            suggested = suggested_sources(user_topics)
        out[name] = name in suggested
    return out


def sources_card_context(user: User) -> dict:
    """Interest-aware source picker context (Faz 3 Bölüm C) — shared by the
    dashboard index, its HTMX partial re-render, and the papers feed sidebar.
    Groups the deployment's enabled sources into "suggested for you" (topics
    overlap the user's classified interests) vs "other", and folds explicit
    `UserSource` overrides in for each source's on/off toggle state.

    Lives here (not in the dashboard module) because scrape owns sources —
    dashboard just renders this card, it never computes source state itself.
    """
    from app.modules.scrape.ai_service import classify_user_topics

    # Local: `feeds` imports this module, so a top-level import would be circular.
    from app.modules.scrape.service.channels import list_user_channels
    from app.modules.scrape.service.feeds import list_user_feeds
    from app.modules.scrape.sources import source_options, suggested_sources

    sources = source_options()
    user_topics = classify_user_topics(user)
    prefs = effective_source_prefs(user, user_topics=user_topics)
    suggested_names = suggested_sources(user_topics)

    for opt in sources:
        opt["is_on"] = prefs.get(opt["name"], True)
        opt["is_suggested"] = opt["name"] in suggested_names

    # Own-channel counts, for the card's "Your feeds" summary block — mirrors
    # feed_count/active_feed_count, which come from scan_status_context, not
    # here; kept together with the channel counts so both dashboard.index and
    # scrape.feed (the two callers that render _sources_card.html) get them
    # for free by merging this context in, same as they already do for feeds.
    channels = list_user_channels(user)
    active_channel_count = sum(1 for c in channels if c.active)
    active_curated_count = sum(1 for s in sources if s["is_on"])

    # `active_source_count` (below) undercounts "what will actually be
    # scanned for me": it's the curated deployment sources only, and never
    # included the user's own RSS feeds or YouTube channels even though both
    # are scanned every run. `active_feed_count` normally comes from
    # scan_status_context, not here, but the honest total needs it — one more
    # cheap indexed query beats leaning on the caller to add three numbers
    # together in a template (the "Your feeds" badge above already does that
    # arithmetic in Jinja; this key exists so the home-page pill doesn't have
    # to).
    active_feed_count = sum(1 for f in list_user_feeds(user) if f.active)

    return {
        "sources": sources,
        "suggested_sources": [s for s in sources if s["is_suggested"]],
        "other_sources": [s for s in sources if not s["is_suggested"]],
        "active_source_count": active_curated_count,
        "user_topics": user_topics,
        "channel_count": len(channels),
        "active_channel_count": active_channel_count,
        "scanned_source_count": active_curated_count + active_feed_count + active_channel_count,
    }


def user_enabled_sources(user: User):
    """The deployment's enabled sources, filtered to this user's effective
    preferences — explicit `UserSource` overrides win; otherwise academic
    sources default on and feed sources default on only when their topics
    match the user's interests (see `effective_source_prefs`)."""
    prefs = effective_source_prefs(user)
    return {name: mod for name, mod in enabled_sources().items() if prefs.get(name, True)}


def set_user_source(user: User, source_name: str, enabled: bool) -> bool:
    """Upsert this user's preference for one source. Idempotent. Returns the
    stored enabled value."""
    row = UserSource.query.filter_by(user_id=user.id, source_name=source_name).first()
    if row is None:
        row = UserSource(user_id=user.id, source_name=source_name, enabled=bool(enabled))
        db.session.add(row)
    else:
        row.enabled = bool(enabled)
    db.session.commit()
    return row.enabled
