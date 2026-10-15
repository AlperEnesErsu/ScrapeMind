"""Report create / detail / status / delete handlers."""

from __future__ import annotations

import structlog
from flask import abort, flash, redirect, render_template, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.modules.scrape import routes
from app.modules.scrape.forms import (
    ReportForm,
)
from app.modules.scrape.routes import scrape_bp

logger = structlog.get_logger()


# ----------------------------------------------------------------------------
# Retrospective reports (Faz 6)
# ----------------------------------------------------------------------------


def _report_create_form() -> ReportForm:
    """A `ReportForm` pre-filled from the user's own keyword dictionary on a
    plain GET. Left untouched on a POST — `FlaskForm` already re-populates
    itself from `request.form` in that case, and overwriting `.data` here
    would just discard whatever the user actually submitted.
    """
    from app.modules.academic.service import list_user_keywords

    form = ReportForm()
    if not form.is_submitted():
        form.keywords.data = ", ".join(kw.value for kw in list_user_keywords(current_user))
    return form


@scrape_bp.route("/reports", methods=["GET"])
@login_required
def reports():
    """Report archive + the "new report" form (Faz 6)."""
    from app.modules.scrape.report_service import MAX_REPORTS_PER_DAY, list_user_reports

    return render_template(
        "reports/index.html",
        form=routes._report_create_form(),
        reports_list=list_user_reports(current_user),
        max_reports_per_day=MAX_REPORTS_PER_DAY,
    )


@scrape_bp.route("/reports/new", methods=["POST"])
@login_required
def report_create():
    """Validate + queue a new "topic" or "learning_map" report (the form's
    `kind`; `create_report` validates both the same way).

    `create_report` never raises — a rejected request (bad year range, no
    keywords, the daily cap) comes back as `(None, message)`, same contract
    `follow_author` uses, so this route never needs a try/except around it.
    """
    from flask import Response

    from app.modules.scrape.report_service import create_report
    from app.tasks.report_tasks import generate

    form = routes._report_create_form()
    if not form.validate_on_submit():
        flash(_("Please correct the errors below."), "danger")
        return redirect(url_for("scrape.reports"))

    keywords = [kw.strip() for kw in form.keywords.data.split(",") if kw.strip()]
    try:
        years = int(form.years.data)
    except (TypeError, ValueError):
        years = None

    report, error = create_report(
        current_user, form.kind.data or "topic", {"keywords": keywords, "years": years}
    )
    if report is None:
        flash(_(error), "danger")
        return redirect(url_for("scrape.reports"))

    generate.delay(report.id)
    log_action(
        "report.created",
        entity_type="report",
        entity_id=str(report.id),
        changes={"kind": report.kind, "years": years},
    )

    if routes._is_htmx():
        # 202 (accepted, work queued) + HX-Redirect rather than a swapped
        # partial: the new report has nothing to show yet but its own status
        # pill, and report_detail already renders + polls that.
        return Response(
            "",
            status=202,
            headers={"HX-Redirect": url_for("scrape.report_detail", report_id=report.id)},
        )
    flash(_("Report queued — it will appear here once generated."), "success")
    return redirect(url_for("scrape.report_detail", report_id=report.id))


@scrape_bp.route("/reports/<int:report_id>", methods=["GET"])
@login_required
def report_detail(report_id: int):
    """Report view, branching on `kind` for the sections partial.

    Ownership goes through `report_service.get_report(current_user, ...)` —
    someone else's report id is a 404, not a 403, so it doesn't confirm the
    id exists (same reasoning `REPORT_AUTHOR_GROUP_EMPTY_MESSAGE` reuses
    `GROUP_NOT_FOUND_MESSAGE` for in `report_service`).
    """
    from app.modules.scrape.report_service import get_report

    report = get_report(current_user, report_id)
    if report is None:
        abort(404)
    steps: list[dict] = []
    canvas: dict | None = None
    if report.kind == "learning_map":
        from app.modules.scrape.learning_map import learning_map_canvas, learning_map_steps

        graph = (report.stats or {}).get("graph")
        steps = learning_map_steps(graph, (report.sections or {}).get("narration"))
        canvas = learning_map_canvas(graph, steps)
    return render_template(
        "reports/detail.html",
        report=report,
        learning_map_steps=steps,
        learning_map_canvas=canvas,
    )


@scrape_bp.route("/reports/<int:report_id>/status", methods=["GET"])
@login_required
def report_status(report_id: int):
    """HTMX poll partial for the detail page's status pill.

    Once the report leaves `pending`/`running`, `_report_status.html` is
    rendered with no `hx-trigger` (polling stops on its own — same contract
    `dashboard/_scan_status.html` uses), and `HX-Refresh` tells the browser
    to reload the whole detail page so the now-available `stats`/`sections`
    actually render, mirroring `scrape_status_poll`'s identical trick for
    the manual-scrape spinner.
    """
    from flask import make_response

    from app.modules.scrape.report_service import get_report

    report = get_report(current_user, report_id)
    if report is None:
        abort(404)
    response = make_response(render_template("reports/_report_status.html", report=report))
    if report.status not in ("pending", "running"):
        response.headers["HX-Refresh"] = "true"
    return response


@scrape_bp.route("/reports/<int:report_id>/delete", methods=["POST"])
@login_required
def report_delete(report_id: int):
    """Delete a report. 404s (not a silent no-op) when the id isn't this
    user's or doesn't exist — same ownership-hiding shape as every other
    delete route in this module."""
    from app.modules.scrape.report_service import delete_report, get_report

    report = get_report(current_user, report_id)
    if report is None:
        abort(404)

    delete_report(current_user, report_id)
    log_action("report.deleted", entity_type="report", entity_id=str(report_id))

    if routes._is_htmx():
        from flask import Response

        return Response("", status=200, headers={"HX-Redirect": url_for("scrape.reports")})
    flash(_("Report deleted."), "info")
    return redirect(url_for("scrape.reports"))
