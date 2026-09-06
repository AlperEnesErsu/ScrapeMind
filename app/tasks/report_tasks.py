"""Retrospective report generation (Faz 6) — `reports.generate`.

On-demand, not scheduled: a user submits a report request via
`report_service.create_report` (writes a `pending` `Report` row), and the
route queues exactly this one task for that row. No `BEAT_SCHEDULE` entry and
no fan-out parent here, unlike `scrape_tasks`/`patent_tasks`/`digest_tasks`.

Same six-step shape as `patent_tasks.ingest_for_user`: guard the row → claim
the user's lock → do the work → `self.retry` on an unexpected failure →
release the lock in `finally`, on every exit path including a retry
(`scrape_tasks.py`'s `run_for_user` docstring/comment on `release_user_lock`
explains why a retry that forgets to release blocks the user for the whole
backoff window — the same failure mode applies here).
"""

from __future__ import annotations

import structlog
from flask import current_app
from flask_babel import force_locale
from flask_babel import gettext as _

from app.core.i18n.utils import SUPPORTED_LOCALES
from app.tasks import celery_app

logger = structlog.get_logger()


@celery_app.task(
    bind=True,
    name="reports.generate",
    max_retries=2,
    soft_time_limit=900,
    time_limit=1200,
)
def generate(self, report_id: int) -> dict:
    """Run one `Report` row end to end and notify its owner.

    `soft_time_limit=900` — three times `digest_tasks.run_for_user`'s
    implicit budget and the `scrape.run_for_user` scan's 300s: a report is a
    multi-page OpenAlex fetch (stats aggregations plus up to
    `openalex_source.REPORT_MAX_WORKS` works) *and* a batch of
    `ai_service.summarize_report_chunk` map calls — one per
    `report_service.REPORT_CHUNK_ITEMS`-sized year chunk — followed by one
    `synthesize_report` reduce call. That's several LLM round-trips per run
    where a digest spends exactly one, so it needs a wider window before
    Celery's soft/hard limits kill it mid-run.

    The task signature only carries `report_id`, not `user_id`, because the
    route that queues this already validated ownership when it loaded the
    row to build the request — the task re-derives the owner from the row
    itself (see below) rather than trusting a second, unverified id.

    Idempotent against Celery's at-least-once delivery (`task_acks_late=True`,
    see `app/tasks/__init__.py`): if this message is redelivered after an
    earlier attempt already ran `run_report` to a terminal status, this call
    is a no-op rather than spending a second OpenAlex/LLM budget on the same
    row.
    """
    from app.core.models.notification import add_notification
    from app.core.models.user import User
    from app.modules.scrape.models import Report
    from app.modules.scrape.report_service import get_report, run_report
    from app.modules.scrape.service import acquire_user_lock, release_user_lock

    # `report_service.get_report(user, report_id)` needs a `user` to filter
    # by, and all we were handed is `report_id` — so a direct, unfiltered
    # lookup is the only way to learn *whose* report this is before we can
    # call the contracted lookup at all. Deleted-user rows and (defensively)
    # any row `get_report` then refuses to hand back under that user both
    # collapse into the same "nothing to do" outcome.
    row = Report.query.get(report_id)
    if row is None:
        logger.warning("report_generate_row_missing", report_id=report_id)
        return {"reason": "report_missing"}

    user = User.query.filter_by(id=row.user_id, deleted_at=None).first()
    if user is None:
        logger.warning("report_generate_user_missing", report_id=report_id, user_id=row.user_id)
        return {"reason": "report_missing"}

    report = get_report(user, report_id)
    if report is None:
        logger.warning("report_generate_ownership_mismatch", report_id=report_id, user_id=user.id)
        return {"reason": "report_missing"}

    if not acquire_user_lock(user.id, "report"):
        logger.info("report_generate_skip_locked", user_id=user.id, report_id=report_id)
        return {"reason": "locked"}

    try:
        if report.status in ("ok", "partial", "error"):
            logger.info(
                "report_generate_skip_already_done",
                report_id=report_id,
                status=report.status,
            )
            return {"reason": "already_done"}

        result = run_report(report)

        # Notification must speak the recipient's language, not the worker's
        # — this is a per-recipient send with no request-scoped locale to
        # inherit, same reasoning as `digest_tasks.run_for_user`. Fall back
        # to the app default when the stored locale isn't one we ship.
        locale = (
            user.locale
            if user.locale in SUPPORTED_LOCALES
            else current_app.config.get("BABEL_DEFAULT_LOCALE", "tr")
        )
        # `.rstrip("/")` so the joined path never doubles a slash; built from
        # config rather than a hardcoded host — `digest_tasks` once shipped a
        # localhost link to production and this must not repeat that.
        base_url = current_app.config["APP_BASE_URL"].rstrip("/")
        with force_locale(locale):
            title = _("Report ready")
            message = _('Your report "%(title)s" has finished generating.') % {
                "title": report.title
            }
            add_notification(
                user.id,
                title=title,
                message=f"{message} {base_url}/papers/reports/{report.id}",
            )

        logger.info(
            "report_generate_done",
            report_id=report_id,
            user_id=user.id,
            status=result.get("status"),
        )
        return result
    except Exception as exc:  # noqa: BLE001 — unexpected failure, retry with backoff
        logger.exception("report_generate_failed", report_id=report_id, user_id=user.id)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)
    finally:
        # Released on every exit from the try block above, including ahead
        # of a retry: the retry fires later (after the backoff), and a lock
        # held across that wait would block the user from starting any other
        # report for the whole window — see `scrape_tasks.run_for_user`'s
        # identical comment on this exact mistake.
        release_user_lock(user.id, "report")
