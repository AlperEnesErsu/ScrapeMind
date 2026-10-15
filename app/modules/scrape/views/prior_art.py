"""Prior-art search handler."""

from __future__ import annotations

import structlog
from flask import render_template
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape import routes
from app.modules.scrape.routes import scrape_bp

logger = structlog.get_logger()


# ----------------------------------------------------------------------------
# Prior-art search (Faz 5.2)
# ----------------------------------------------------------------------------

#: How many words of the idea become search terms. The patent APIs match on
#: title/abstract text, so a whole paragraph would over-constrain EPO's CQL
#: (`ti,ab any` per term, OR-ed) into noise.
_PRIOR_ART_MAX_TERMS = 6


#: Turkish and English function words carry no signal for a patent search and
#: would each become their own OR clause. Deliberately a small hand-list, not
#: an NLP dependency — the idea text is short and the failure mode of a missed
#: stopword is one wasted clause.
_PRIOR_ART_STOPWORDS = {
    "bir", "ve", "veya", "ile", "için", "bu", "şu", "olan", "olarak", "daha",
    "gibi", "kadar", "her", "the", "and", "or", "with", "for", "this", "that",
    "a", "an", "of", "to", "in", "on", "is", "are", "be", "as", "by", "from",
}  # fmt: skip


def _prior_art_terms(idea: str) -> list[str]:
    """Search terms derived from a free-text invention description.

    The form asks for the idea once because the LLM assessment needs the prose
    while the adapters need keywords; this is the bridge. Longest words first:
    in a sentence like "a system for detecting bearing wear with vibration
    sensors", the long words are the technical ones.
    """
    import re

    words = re.findall(r"[\w-]{3,}", (idea or "").lower(), flags=re.UNICODE)
    seen: set[str] = set()
    terms: list[str] = []
    for word in sorted(words, key=len, reverse=True):
        if word in routes._PRIOR_ART_STOPWORDS or word in seen:
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) >= routes._PRIOR_ART_MAX_TERMS:
            break
    return terms


@scrape_bp.route("/patents", methods=["GET", "POST"])
@login_required
def prior_art():
    """Live prior-art search over the patent sources, plus an optional LLM
    novelty assessment.

    **Nothing here is persisted.** `service.search_patents_live` returns
    payloads rather than Paper rows: someone checking twenty wordings of an
    invention would otherwise leave hundreds of rows in `papers` that no
    user's feed ever wanted, and EPO's fair-use terms are happier with a
    search that keeps nothing. Only the nightly keyword scan persists.

    The assessment is optional and its absence is not an error — the patent
    list is the part with evidentiary value; the LLM output is a reading aid
    over it.
    """
    from flask import current_app

    from app.modules.scrape.ai_service import analyze_novelty, is_ai_enabled
    from app.modules.scrape.forms import PriorArtForm
    from app.modules.scrape.service import patent_sources, search_patents_live

    form = PriorArtForm()
    sources = patent_sources()
    ctx = {
        "form": form,
        "sources": sources,
        "ai_available": is_ai_enabled(current_user),
        "results": None,
        "per_source": {},
        "assessment": None,
        "terms": [],
        # The patent tracking module (Faz 8) searches claims and descriptions
        # of recent US grants locally. Offered here because the two searches
        # answer different questions -- this one live, worldwide and saving
        # nothing, that one local, US-only and deep -- and neither page knew
        # the other existed. Checked against `view_functions`, not assumed: a
        # deployment without the module must not get a link that 404s. Set
        # before the no-sources return, which is where it helps most: no
        # EPO/PatentsView keys, but a local corpus to search.
        "patent_search_available": "patent.search" in current_app.view_functions,
    }

    if not sources:
        # Keys missing, or the admin has not switched patents on. Say which,
        # rather than rendering an empty search box that silently finds
        # nothing — see the gates in docs/SCRAPING.md §5.
        return render_template("scrape/patents.html", **ctx)

    if form.validate_on_submit():
        idea = form.idea.data.strip()
        terms = routes._prior_art_terms(idea)
        results, per_source = search_patents_live(terms)
        ctx.update(results=results, per_source=per_source, terms=terms)

        if form.assess.data and results:
            # A missing assessment must not cost the user their results, which
            # are already in hand and are the useful half.
            try:
                ctx["assessment"] = analyze_novelty(idea, results, user=current_user)
            except Exception:  # noqa: BLE001
                logger.exception("prior_art_assessment_failed", user_id=current_user.id)

        log_action("patents.prior_art_search", entity_type="patent_search", entity_id=None)

    return render_template("scrape/patents.html", **ctx)
