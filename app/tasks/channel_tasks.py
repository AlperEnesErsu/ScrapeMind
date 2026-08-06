"""YouTube channel tasks — per-user ingestion + per-video transcript
summarization.

Two distinct steps, mirroring the RSS feed / relevance-scoring split in
`feed_tasks.py`:

  * `ingest_for_user` — fetches this user's subscribed channels (see
    `service.ingest_user_channels`), upserts new Papers (kind="video"), then
    dispatches at most `CHANNEL_SUMMARY_MAX_PER_RUN` `summarize_video` jobs
    for the videos it just created. One model call per video per user per
    night is real spend, so this cap keeps a free-tier model's rate limit
    and a user's token budget sane even for a user subscribed to a dozen
    active channels.
  * `summarize_video` — one video, one LLM call. Runs on the `llm` queue,
    same as `feeds.link_for_user` and `digest.run_for_user`.

Beat calls `ingest_for_all_users` nightly, between `feeds-ingest-nightly`
(02:45) and `scrape-arxiv-nightly` (03:15) — see `app/tasks/schedule.py`.
"""

from __future__ import annotations

import structlog
from flask import current_app

from app.core.models.user import User
from app.tasks import celery_app
from app.tasks.fanout import fan_out

logger = structlog.get_logger()


@celery_app.task(name="channels.ingest_for_user", bind=True, max_retries=2)
def ingest_for_user(self, user_id: int, *, trigger: str = "auto", lock_held: bool = False) -> dict:
    """Ingest this user's subscribed channels, then fan out a bounded number
    of transcript-summarization jobs for whatever Papers were newly created.

    `lock_held`/`trigger` follow the same contract as `scrape_tasks.run_for_user`.
    """
    from app.modules.scrape.service import (
        acquire_user_lock,
        apply_scan_result,
        ingest_user_channels,
        record_scan_run,
        release_user_lock,
    )

    user = User.query.filter_by(id=user_id, deleted_at=None).first()
    if user is None:
        logger.warning("channel_ingest_user_missing", user_id=user_id)
        release_user_lock(user_id, "channels")
        return {"reason": "user_missing"}

    if not lock_held and not acquire_user_lock(user_id, "channels"):
        logger.info("channel_ingest_skip_locked", user_id=user_id)
        return {"reason": "already_running"}

    try:
        with record_scan_run(user_id, "channels", trigger=trigger) as run:
            summary, new_paper_ids = ingest_user_channels(user)
            hits = sum(v for v in summary.values() if v > 0)
            apply_scan_result(run, {"sources": summary, "hits": hits, "new": len(new_paper_ids)})

        max_per_run = int(current_app.config.get("CHANNEL_SUMMARY_MAX_PER_RUN", 5))
        for paper_id in new_paper_ids[:max_per_run]:
            # Carry the subscriber through: AI is off by default deployment-wide
            # and many users supply their own OpenRouter key instead
            # (ai_service._resolve_llm prefers the per-user key). Without the
            # user id the summary would only ever work when a global key is set.
            summarize_video.delay(paper_id, user_id=user_id)

        return {"sources": summary, "hits": hits, "new": len(new_paper_ids)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("channel_ingest_failed", user_id=user_id)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)
    finally:
        release_user_lock(user_id, "channels")


@celery_app.task(
    name="channels.summarize_video",
    bind=True,
    max_retries=2,
    soft_time_limit=240,
    time_limit=300,
)
def summarize_video(self, paper_id: int, user_id: int | None = None) -> dict:
    """Fetch a video's transcript and generate its LLM summary.

    `user_id` is the subscriber whose ingestion surfaced this video. It only
    selects *whose* LLM credentials pay for the call — AI is off by default
    deployment-wide and users may supply their own OpenRouter key, which
    `ai_service._resolve_llm` prefers over the global one. The resulting
    summary is not per-user: VideoSummary is unique on paper_id, so it is a
    shared cache every subscriber to that video reads.

    Several outcomes here are valid, not errors — a video with no summary is
    a fine result, so these all degrade silently (log + return, never raise
    or retry): the paper doesn't exist, it isn't a video, it already has a
    summary, AI is disabled, or no transcript could be fetched. Only a
    genuinely unexpected exception falls through to the retry below.
    """
    from app.modules.scrape import ai_service
    from app.modules.scrape.models import Paper
    from app.modules.scrape.sources.youtube_channel_source import fetch_transcript

    try:
        user = (
            User.query.filter_by(id=user_id, deleted_at=None).first()
            if user_id is not None
            else None
        )
        paper = Paper.query.filter_by(id=paper_id, deleted_at=None).first()
        if paper is None:
            logger.info("channel_summarize_paper_missing", paper_id=paper_id)
            return {"reason": "paper_missing"}
        if paper.kind != "video":
            logger.info("channel_summarize_not_a_video", paper_id=paper_id, kind=paper.kind)
            return {"reason": "not_a_video"}
        if ai_service.get_video_summary(paper) is not None:
            logger.info("channel_summarize_already_summarized", paper_id=paper_id)
            return {"reason": "already_summarized"}
        if not ai_service.is_ai_enabled(user):
            logger.info("channel_summarize_ai_disabled", paper_id=paper_id, user_id=user_id)
            return {"reason": "ai_disabled"}

        transcript = fetch_transcript(paper.external_id)
        if transcript is None:
            logger.info("channel_summarize_no_transcript", paper_id=paper_id)
            return {"reason": "no_transcript"}

        summary = ai_service.generate_video_summary(paper, transcript, user=user)
        if summary is None:
            logger.info("channel_summarize_generation_failed", paper_id=paper_id)
            return {"reason": "generation_failed"}
        return {"paper_id": paper_id, "summary_id": summary.id}
    except Exception as exc:  # noqa: BLE001 — only a genuinely unexpected failure retries
        logger.exception("channel_summarize_video_failed", paper_id=paper_id)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1) ** 2)


@celery_app.task(name="channels.ingest_for_all_users")
def ingest_for_all_users() -> dict:
    """Fan out per-user channel ingestion. Beat calls this once a night.

    Spread over SCAN_FANOUT_WINDOW_SECONDS exactly like
    `scrape.run_for_all_users`/`feeds.link_for_all_users` — see
    `app/tasks/fanout.py`.
    """
    queued = fan_out(ingest_for_user)
    return {"queued": queued}
