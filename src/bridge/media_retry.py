"""MAX media retry helpers."""

import asyncio
from contextlib import suppress
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Optional

from . import forwarding as bridge_forwarding
from . import mapping as bridge_mapping
from .contracts import MaxAttachment, MaxAttachmentFailure, MaxBridgePort, TelegramBridgePort
from .retry_policy import exponential_backoff_seconds
from ..config.loader import AppConfig
from ..db.repository import PendingMediaDownload, Repository
from ..logging_utils import build_max_flow_id, log_event

logger = logging.getLogger("src.bridge.core")

SendAttachment = Callable[[int, MaxAttachment, str], Awaitable[Optional[int]]]


def is_retryable_media_failure(failure: MaxAttachmentFailure) -> bool:
    if not (
        failure.retryable
        and failure.reference_id
        and failure.media_chat_id
        and failure.media_msg_id
    ):
        return False
    if failure.kind == "video":
        return failure.reference_kind == "video_id"
    if failure.kind == "audio":
        return failure.reference_kind in {"audio_id", "file_id"}
    return False


def pending_media_retry_delay(attempts_after_failure: int) -> int:
    # Бесконечный retry с cap: 1m, 2m, 4m ... до 6h.
    return exponential_backoff_seconds(
        attempts_after_failure,
        base_seconds=60,
        cap_seconds=6 * 3600,
        max_exponent=8,
    )


async def enqueue_retryable_media_failures(
    *,
    repo: Repository,
    msg,
    topic_id: int,
    flow_id: str | None = None,
) -> tuple[int, list[MaxAttachmentFailure]]:
    enqueued = 0
    display_failures: list[MaxAttachmentFailure] = []
    now = int(time.time())
    first_retry_at = now + 60
    for failure in msg.attachment_failures:
        if not is_retryable_media_failure(failure):
            display_failures.append(failure)
            continue

        existing = await find_existing_pending_media_for_failure(
            repo=repo,
            msg=msg,
            failure=failure,
        )
        if existing is not None:
            log_event(
                logger,
                logging.INFO,
                "bridge.media_retry.enqueued",
                flow_id=flow_id,
                direction="inbound",
                stage="media_retry",
                outcome="existing",
                reason="pending_media_already_exists",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=topic_id,
                pending_media_id=existing.id,
                attachment_index=failure.index,
                kind=failure.kind,
                reference_kind=failure.reference_kind,
            )
            continue

        job_id = await repo.enqueue_pending_media(
            PendingMediaDownload(
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=topic_id,
                attachment_index=failure.index,
                kind=failure.kind,
                source_type=failure.source_type,
                media_chat_id=failure.media_chat_id or msg.chat_id,
                media_msg_id=failure.media_msg_id or msg.msg_id,
                reference_kind=failure.reference_kind or "video_id",
                reference_id=failure.reference_id or "",
                filename=failure.filename,
                duration=failure.duration,
                width=failure.width,
                height=failure.height,
                next_attempt_at=first_retry_at,
                last_error=failure.reason,
            )
        )
        enqueued += 1
        display_failures.append(failure)
        log_event(
            logger,
            logging.INFO,
            "bridge.media_retry.enqueued",
            flow_id=flow_id,
            direction="inbound",
            stage="media_retry",
            outcome="enqueued",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=topic_id,
            pending_media_id=job_id,
            attachment_index=failure.index,
            kind=failure.kind,
            reference_kind=failure.reference_kind,
        )
    return enqueued, display_failures


async def find_existing_pending_media_for_failure(
    *,
    repo: Repository,
    msg,
    failure: MaxAttachmentFailure,
) -> PendingMediaDownload | None:
    finder = getattr(repo, "find_active_pending_media", None)
    if callable(finder):
        existing = await finder(
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            attachment_index=failure.index,
            kind=failure.kind,
        )
        if existing is not None:
            return existing

    ref_finder = getattr(repo, "find_active_pending_media_by_reference", None)
    if (
        callable(ref_finder)
        and failure.reference_kind
        and failure.reference_id
        and (failure.media_chat_id or msg.chat_id)
        and (failure.media_msg_id or msg.msg_id)
    ):
        return await ref_finder(
            media_chat_id=failure.media_chat_id or msg.chat_id,
            media_msg_id=failure.media_msg_id or msg.msg_id,
            attachment_index=failure.index,
            kind=failure.kind,
            reference_kind=failure.reference_kind,
            reference_id=failure.reference_id,
        )
    return None


async def mark_pending_media_retry(
    *,
    repo: Repository,
    job: PendingMediaDownload,
    error: str,
    flow_id: str,
):
    if job.id is None:
        return
    attempts_after_failure = int(job.attempts or 0) + 1
    delay = pending_media_retry_delay(attempts_after_failure)
    next_attempt_at = int(time.time()) + delay
    await repo.mark_pending_media_retry(
        job.id,
        error=error,
        next_attempt_at=next_attempt_at,
    )
    log_event(
        logger,
        logging.WARNING,
        "bridge.media_retry.retry_scheduled",
        flow_id=flow_id,
        direction="inbound",
        stage="media_retry",
        outcome="retry",
        reason=error,
        max_chat_id=job.max_chat_id,
        max_msg_id=job.max_msg_id,
        tg_topic_id=job.tg_topic_id,
        pending_media_id=job.id,
        attachment_index=job.attachment_index,
        attempts=attempts_after_failure,
        retry_in_seconds=delay,
    )


async def process_pending_media_download(
    *,
    cfg: AppConfig,
    repo: Repository,
    max_adapter: MaxBridgePort,
    tg: TelegramBridgePort,
    job: PendingMediaDownload,
):
    flow_id = build_max_flow_id(
        job.max_chat_id,
        f"{job.max_msg_id}:media:{job.attachment_index}",
    ) or "mx:pending-media"
    if not job.id:
        return
    if not job.reference_id or not (
        (job.kind == "video" and job.reference_kind == "video_id")
        or (job.kind == "audio" and job.reference_kind in {"audio_id", "file_id"})
    ):
        await repo.mark_pending_media_failed(
            job.id,
            error="missing_stable_media_reference",
        )
        log_event(
            logger,
            logging.ERROR,
            "bridge.media_retry.failed",
            flow_id=flow_id,
            direction="inbound",
            stage="media_retry",
            outcome="failed",
            reason="missing_stable_media_reference",
            max_chat_id=job.max_chat_id,
            max_msg_id=job.max_msg_id,
            pending_media_id=job.id,
        )
        return

    log_event(
        logger,
        logging.INFO,
        "bridge.media_retry.attempt_started",
        flow_id=flow_id,
        direction="inbound",
        stage="media_retry",
        outcome="started",
        max_chat_id=job.max_chat_id,
        max_msg_id=job.max_msg_id,
        tg_topic_id=job.tg_topic_id,
        pending_media_id=job.id,
        attachment_index=job.attachment_index,
        attempts=int(job.attempts or 0) + 1,
        kind=job.kind,
        reference_kind=job.reference_kind,
    )

    if job.kind == "audio":
        download_media = max_adapter.download_audio_reference
    else:
        download_media = max_adapter.download_video_reference

    try:
        if job.kind == "audio":
            attachment = await download_media(
                chat_id=job.media_chat_id,
                msg_id=job.media_msg_id,
                reference_id=job.reference_id,
                reference_kind=job.reference_kind,
                attachment_index=job.attachment_index,
                filename_hint=job.filename,
                duration=job.duration,
                source_type=job.source_type or "AUDIO",
                flow_id=flow_id,
            )
        else:
            attachment = await download_media(
                chat_id=job.media_chat_id,
                msg_id=job.media_msg_id,
                video_id=job.reference_id,
                attachment_index=job.attachment_index,
                filename_hint=job.filename,
                duration=job.duration,
                width=job.width,
                height=job.height,
                source_type=job.source_type or "VIDEO",
                flow_id=flow_id,
            )
    except Exception as e:
        await mark_pending_media_retry(
            repo=repo,
            job=job,
            error=f"download_exception:{e.__class__.__name__}",
            flow_id=flow_id,
        )
        return
    if attachment is None:
        await mark_pending_media_retry(
            repo=repo,
            job=job,
            error="download_failed",
            flow_id=flow_id,
        )
        return

    try:
        if bridge_forwarding.is_file_too_large(cfg, attachment.local_path):
            placeholder = cfg.content.placeholder_file_too_large.format(
                filename=attachment.filename or Path(attachment.local_path).name
            )
            await tg.send_text(job.tg_topic_id, placeholder, flow_id=flow_id)
            await repo.mark_pending_media_failed(
                job.id,
                error="file_too_large",
            )
            log_event(
                logger,
                logging.ERROR,
                "bridge.media_retry.failed",
                flow_id=flow_id,
                direction="inbound",
                stage="media_retry",
                outcome="failed",
                reason="file_too_large",
                max_chat_id=job.max_chat_id,
                max_msg_id=job.max_msg_id,
                tg_topic_id=job.tg_topic_id,
                pending_media_id=job.id,
                attachment_index=job.attachment_index,
            )
            return

        media_label = "голосовое" if job.kind == "audio" else "видео"
        caption = f"Докачанное {media_label} MAX #{job.attachment_index + 1}"
        try:
            tg_msg_id = await bridge_forwarding.send_attachment(
                cfg=cfg,
                tg=tg,
                topic_id=job.tg_topic_id,
                attachment=attachment,
                caption=caption,
                flow_id=flow_id,
            )
        except Exception as e:
            await mark_pending_media_retry(
                repo=repo,
                job=job,
                error=f"tg_send_exception:{e.__class__.__name__}",
                flow_id=flow_id,
            )
            return
        if not tg_msg_id:
            await mark_pending_media_retry(
                repo=repo,
                job=job,
                error="tg_send_failed",
                flow_id=flow_id,
            )
            return

        await bridge_mapping.save_tg_reply_mapping(
            repo,
            tg_msg_id=tg_msg_id,
            max_chat_id=job.max_chat_id,
            max_msg_id=job.max_msg_id,
            tg_topic_id=job.tg_topic_id,
            source="pending_media",
        )
        await repo.mark_pending_media_delivered(
            job.id,
            tg_msg_id=tg_msg_id,
        )
        log_event(
            logger,
            logging.INFO,
            "bridge.media_retry.delivered",
            flow_id=flow_id,
            direction="inbound",
            stage="media_retry",
            outcome="delivered",
            max_chat_id=job.max_chat_id,
            max_msg_id=job.max_msg_id,
            tg_topic_id=job.tg_topic_id,
            tg_msg_id=tg_msg_id,
            pending_media_id=job.id,
            attachment_index=job.attachment_index,
            attempts=int(job.attempts or 0) + 1,
        )
    finally:
        with suppress(Exception):
            Path(attachment.local_path).unlink(missing_ok=True)


async def run_pending_media_downloads(
    *,
    repo: Repository,
    cfg: AppConfig,
    max_adapter: MaxBridgePort,
    tg: TelegramBridgePort,
    poll_interval: int = 60,
    lease_seconds: int = 600,
):
    log_event(
        logger,
        logging.INFO,
        "bridge.media_retry.worker_started",
        stage="media_retry",
        outcome="started",
        poll_interval_seconds=poll_interval,
        lease_seconds=lease_seconds,
    )
    while True:
        try:
            now = int(time.time())
            jobs = await repo.get_due_pending_media(now=now, limit=5)
            for job in jobs:
                if not job.id:
                    continue
                leased = await repo.lease_pending_media(
                    job.id,
                    lease_until=now + lease_seconds,
                    now=now,
                )
                if not leased:
                    continue
                await process_pending_media_download(
                    cfg=cfg,
                    repo=repo,
                    max_adapter=max_adapter,
                    tg=tg,
                    job=job,
                )
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "bridge.media_retry.worker_failed",
                stage="media_retry",
                outcome="failed",
                error=str(e),
            )
        await asyncio.sleep(poll_interval)
