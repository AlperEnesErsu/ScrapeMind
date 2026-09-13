"""Patent tracking pages.

Two routes for now: the window itself, and one patent read the way a patent is
actually read — claim 1 first, then the tree under it. Search arrives in 8.4.

Both must render on an empty corpus. Until the weekly load lands in 8.3 the
corpus *is* empty, and a page that only works once data exists cannot be
reviewed before then.
"""

import structlog
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import login_required

from app.core.audit.middleware import log_action
from app.core.auth.decorators import permission_required
from app.modules.patent import service

logger = structlog.get_logger()

patent_bp = Blueprint("patent", __name__, template_folder="templates")


@patent_bp.route("/")
@login_required
def index():
    """The window: what is in it, how wide it is, and what arrived last."""
    return render_template(
        "patent/index.html",
        stats=service.window_stats(),
        documents=service.recent_documents(),
        window_start=service.window_start(),
    )


@patent_bp.route("/search")
@login_required
def search():
    """Full-text search over the window, claims as their own scope.

    Declared before `/<doc_number>` for readers; Werkzeug would prefer the
    static rule anyway, so no patent number can shadow it.
    """
    from app.modules.patent import search as patent_search

    filters = patent_search.SearchFilters.from_args(request.args)
    page = request.args.get("page", 1, type=int)
    results = None
    snippets = {}
    similarity: dict[int, float] = {}
    semantic_unavailable = False
    capped_total = None
    if not filters.is_empty:
        if filters.semantic and filters.q:
            from flask_login import current_user

            hybrid = patent_search.hybrid_search(filters, page, user=current_user)
            results = hybrid.pagination
            similarity = hybrid.similarity
            semantic_unavailable = not hybrid.semantic_used
            if hybrid.capped:
                capped_total = hybrid.text_total
        else:
            results = patent_search.build_query(filters).paginate(
                page=page, per_page=patent_search.PER_PAGE, error_out=False
            )
        snippets = patent_search.snippets_for(results.items, filters.q, scope=filters.scope)
        if similarity:
            snippets.update(
                patent_search.scope_claim_snippets(results.items, exclude=set(snippets))
            )
    return render_template(
        "patent/search.html",
        filters=filters,
        results=results,
        snippets=snippets,
        similarity=similarity,
        capped_total=capped_total,
        candidates=patent_search.CANDIDATES,
        # Asked for semantic, got full text only. Said out loud so lexical
        # results are never mistaken for meaning-based ones.
        semantic_unavailable=semantic_unavailable,
        # Text that reduces to stopwords only is ignored by the query; saying
        # so stops "no results" from reading as "nobody claims this".
        # Semantic search does not need searchable words, so the warning only
        # applies when full text is all that ran.
        query_ignored=bool(filters.q)
        and not (filters.semantic and similarity)
        and not patent_search.query_is_meaningful(filters.q),
    )


@patent_bp.route("/<doc_number>")
@login_required
def detail(doc_number: str):
    document = service.get_document(doc_number)
    if document is None:
        abort(404)
    return render_template(
        "patent/detail.html",
        document=document,
        claim_tree=service.claim_tree(document),
    )


# Whitelist, same shape as `tasks_admin._TRIGGERS`: the browser names an
# action, never a task. An arbitrary task name can not be dispatched from here.
_TRIGGERS = {
    "refresh": "patents_bulk.refresh_window",
    "purge": "patents_bulk.purge_window",
}


@patent_bp.route("/admin")
@login_required
@permission_required("patents.manage")
def admin():
    """Window health, what the next load will do, and the last runs.

    Reads the worker state from `system_health` rather than asking Celery
    directly: a queued load with no worker to take it looks exactly like a
    load that is running, and the panel must not let those read the same.
    """
    from app.core.health import system_health

    return render_template(
        "patent/admin.html",
        stats=service.window_stats(),
        settings=service.load_settings(),
        runs=service.recent_runs(),
        worker_ok=system_health().get("worker") == "ok",
    )


@patent_bp.route("/admin/run/<action>", methods=["POST"])
@login_required
@permission_required("patents.manage")
def admin_run(action: str):
    task_name = _TRIGGERS.get(action)
    if task_name is None:
        flash(_("Unknown task."), "danger")
        return redirect(url_for("patent.admin"))

    if action == "refresh":
        from app.modules.patent.uspto import credentials_ok

        if not credentials_ok():
            # The button is disabled in this state; this covers a stale page or
            # a hand-made POST. Queuing it would only produce a skipped run
            # reported here as "queued".
            flash(_("The weekly load needs a USPTO Open Data Portal API key."), "warning")
            return redirect(url_for("patent.admin"))

    from app.tasks import celery_app

    result = celery_app.send_task(task_name)
    task_id = getattr(result, "id", None)
    log_action(
        "patent.manual_trigger",
        entity_type="celery_task",
        entity_id=task_name,
        changes={"task_id": task_id},
    )
    logger.info("patent_manual_trigger", task=task_name, task_id=task_id)

    messages = {
        "refresh": _("Weekly patent load queued."),
        "purge": _("Patent window cleanup queued."),
    }
    flash(messages[action], "success")
    return redirect(url_for("patent.admin"))
