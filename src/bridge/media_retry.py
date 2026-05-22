"""MAX media retry helpers."""

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Optional

from . import forwarding as bridge_forwarding
from . import mapping as bridge_mapping
from .contracts import MaxAttachment, MaxAttachmentFailure, MaxBridgePort, TelegramBridgePort
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
    exponent = max(0, min(attempts_after_failure - 1, 8))
    return min(6 * 3600, 60 * (2 ** exponent))


async def mark_pending_media_retry(
    *,
    repo: Repository,
    job: PendingMediaDownload,
    error: str,
    flow_id: str,
):
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
    )
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

    download_method_name = (
        "download_audio_reference"
        if job.kind == "audio"
        else "download_video_reference"
    )
    download_media = getattr(max_adapter, download_method_name, None)
    if not callable(download_media):
        await mark_pending_media_retry(
            repo=repo,
            job=job,
            error=f"max_adapter_missing_{job.kind}_retry",
            flow_id=flow_id,
        )
        return

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
        try:
            Path(attachment.local_path).unlink(missing_ok=True)
        except Exception:
            pass
