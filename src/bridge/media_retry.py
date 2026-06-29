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
from .contracts import (
    MaxAttachment,
    MaxAttachmentFailure,
    MaxBridgePort,
    TelegramBridgePort,
    is_usable_max_chat_id,
)
from .retry_policy import exponential_backoff_seconds
from ..config.loader import AppConfig
from ..db.repository import PendingMediaDownload, Repository
from ..logging_utils import build_max_flow_id, log_event

logger = logging.getLogger("src.bridge.core")

SendAttachment = Callable[[int, MaxAttachment, str], Awaitable[Optional[int]]]
LATE_DUPLICATE_REFERENCE_KIND = "late_duplicate"
LATE_DUPLICATE_FINAL_DELAY_SECONDS = 180
_EDIT_STATUS_SUFFIXES = (":EDITED", ":MESSAGESTATUS.EDITED")


def media_kind_label(kind: str | None, *, source_type: str | None = None) -> str:
    normalized_kind = str(kind or "").lower()
    normalized_source = str(source_type or "").upper()
    if normalized_kind == "photo":
        return "Фото MAX"
    if normalized_kind == "video":
        return "Видео MAX"
    if normalized_kind == "audio":
        return "Голосовое MAX" if "VOICE" in normalized_source or not normalized_source else "Аудио MAX"
    if normalized_kind == "document":
        return "Файл MAX"
    return "Медиа MAX"


def compose_pending_media_text(failure: MaxAttachmentFailure) -> str:
    label = media_kind_label(failure.kind, source_type=failure.source_type)
    return f"⏳ {label} #{failure.index + 1} загружается и будет дослано через пару минут"


def compose_terminal_media_failure_text(*, kind: str, index: int, source_type: str | None = None) -> str:
    label = media_kind_label(kind, source_type=source_type)
    return f"⚠️ {label} #{index + 1} так и не удалось загрузить автоматически"


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


def is_late_duplicate_finalizer_job(job: PendingMediaDownload) -> bool:
    return job.reference_kind == LATE_DUPLICATE_REFERENCE_KIND


def is_late_duplicate_resolved_delivery(latest_delivery: Optional[dict]) -> bool:
    if not latest_delivery:
        return False
    if latest_delivery.get("status") != "delivered":
        return False
    error = str(latest_delivery.get("error") or "")
    return not error or error in {
        "late_media_recovered",
        "manual_photo_recovery",
        "manual_video_recovery",
        "manual_media_recovery",
    }


def edit_base_max_msg_id(max_msg_id: object, status: object | None = None) -> str | None:
    text = str(max_msg_id or "").strip()
    if not text:
        return None
    status_text = str(status or "").strip().upper()
    if "." in status_text:
        status_text = status_text.rsplit(".", 1)[-1]
    upper_text = text.upper()
    for suffix in _EDIT_STATUS_SUFFIXES:
        if upper_text.endswith(suffix):
            return text[: -len(suffix)] or None
    return text if status_text == "EDITED" else None


def canonical_media_base_msg_id(max_msg_id: object, status: object | None = None) -> str | None:
    return edit_base_max_msg_id(max_msg_id, status) or (str(max_msg_id or "").strip() or None)


def media_part_kind(kind: object) -> str:
    return str(kind or "").strip().lower() or "unknown"


def attachment_part_index(attachment: MaxAttachment, fallback_index: int) -> int:
    try:
        value = getattr(attachment, "attachment_index", None)
        return int(value) if value is not None else int(fallback_index)
    except (TypeError, ValueError):
        return int(fallback_index)


def media_part_reference(source) -> tuple[str | None, str | None]:
    reference_kind = getattr(source, "reference_kind", None)
    reference_id = getattr(source, "reference_id", None)
    if reference_kind and reference_id is not None:
        return str(reference_kind), str(reference_id)
    return None, None


async def has_any_delivered_media_parts(
    *,
    repo: Repository,
    max_chat_id: str,
    base_max_msg_id: str,
) -> bool:
    checker = getattr(repo, "has_delivered_media_parts", None)
    if not callable(checker):
        return False
    return bool(
        await checker(
            max_chat_id=max_chat_id,
            base_max_msg_id=base_max_msg_id,
        )
    )


async def find_delivered_media_part(
    *,
    repo: Repository,
    max_chat_id: str,
    base_max_msg_id: str,
    attachment_index: int,
    kind: str,
    reference_kind: str | None = None,
    reference_id: str | None = None,
):
    finder = getattr(repo, "find_delivered_media_part", None)
    if callable(finder):
        exact = await finder(
            max_chat_id=max_chat_id,
            base_max_msg_id=base_max_msg_id,
            attachment_index=attachment_index,
            kind=media_part_kind(kind),
        )
        if exact is not None:
            return exact
    ref_finder = getattr(repo, "find_delivered_media_part_by_reference", None)
    if callable(ref_finder) and reference_kind and reference_id is not None:
        return await ref_finder(
            max_chat_id=max_chat_id,
            base_max_msg_id=base_max_msg_id,
            kind=media_part_kind(kind),
            reference_kind=reference_kind,
            reference_id=str(reference_id),
        )
    return None


async def is_media_part_delivered(
    *,
    repo: Repository,
    max_chat_id: str,
    base_max_msg_id: str,
    attachment_index: int,
    kind: str,
    reference_kind: str | None = None,
    reference_id: str | None = None,
) -> bool:
    return (
        await find_delivered_media_part(
            repo=repo,
            max_chat_id=max_chat_id,
            base_max_msg_id=base_max_msg_id,
            attachment_index=attachment_index,
            kind=kind,
            reference_kind=reference_kind,
            reference_id=reference_id,
        )
        is not None
    )


async def is_attachment_delivered(
    *,
    repo: Repository,
    msg,
    attachment: MaxAttachment,
    fallback_index: int,
) -> bool:
    base_msg_id = canonical_media_base_msg_id(msg.msg_id, getattr(msg, "status", None))
    if not base_msg_id:
        return False
    reference_kind, reference_id = media_part_reference(attachment)
    return await is_media_part_delivered(
        repo=repo,
        max_chat_id=msg.chat_id,
        base_max_msg_id=base_msg_id,
        attachment_index=attachment_part_index(attachment, fallback_index),
        kind=attachment.kind,
        reference_kind=reference_kind,
        reference_id=reference_id,
    )


async def undelivered_attachments(
    *,
    repo: Repository,
    msg,
    attachments: list[MaxAttachment],
) -> list[tuple[int, MaxAttachment]]:
    result: list[tuple[int, MaxAttachment]] = []
    for fallback_index, attachment in enumerate(attachments):
        part_index = attachment_part_index(attachment, fallback_index)
        if await is_attachment_delivered(
            repo=repo,
            msg=msg,
            attachment=attachment,
            fallback_index=fallback_index,
        ):
            continue
        result.append((part_index, attachment))
    return result


async def is_failure_delivered(
    *,
    repo: Repository,
    msg,
    failure: MaxAttachmentFailure,
) -> bool:
    base_msg_id = canonical_media_base_msg_id(msg.msg_id, getattr(msg, "status", None))
    if not base_msg_id:
        return False
    return await is_media_part_delivered(
        repo=repo,
        max_chat_id=msg.chat_id,
        base_max_msg_id=base_msg_id,
        attachment_index=int(failure.index),
        kind=failure.kind,
        reference_kind=failure.reference_kind,
        reference_id=failure.reference_id,
    )


async def are_failures_delivered_or_legacy_resolved(
    *,
    repo: Repository,
    msg,
    failures: list[MaxAttachmentFailure],
) -> bool:
    if not failures:
        return False
    unresolved = []
    for failure in failures:
        if not await is_failure_delivered(repo=repo, msg=msg, failure=failure):
            unresolved.append(failure)
    if not unresolved:
        return True
    return await is_edit_media_resolved_by_base_delivery(repo=repo, msg=msg)


async def has_active_pending_media_part(
    *,
    repo: Repository,
    msg,
    attachment_index: int,
    kind: str,
) -> bool:
    finder = getattr(repo, "find_active_pending_media", None)
    if not callable(finder):
        return False
    candidate_ids = [str(getattr(msg, "msg_id", "") or "")]
    base_msg_id = canonical_media_base_msg_id(
        getattr(msg, "msg_id", None),
        getattr(msg, "status", None),
    )
    if base_msg_id and base_msg_id not in candidate_ids:
        candidate_ids.append(base_msg_id)
    for candidate_id in candidate_ids:
        if not candidate_id:
            continue
        existing = await finder(
            max_chat_id=msg.chat_id,
            max_msg_id=candidate_id,
            attachment_index=int(attachment_index),
            kind=media_part_kind(kind),
        )
        if existing is not None:
            return True
    return False


async def save_delivered_attachment_part(
    *,
    repo: Repository,
    msg,
    attachment: MaxAttachment,
    tg_msg_id: int,
    tg_topic_id: int,
    source: str,
    fallback_index: int,
    commit: bool = True,
) -> None:
    saver = getattr(repo, "save_delivered_media_part", None)
    if not callable(saver) or not tg_msg_id:
        return
    base_msg_id = canonical_media_base_msg_id(msg.msg_id, getattr(msg, "status", None))
    if not base_msg_id:
        return
    reference_kind, reference_id = media_part_reference(attachment)
    await saver(
        max_chat_id=msg.chat_id,
        base_max_msg_id=base_msg_id,
        attachment_index=attachment_part_index(attachment, fallback_index),
        kind=media_part_kind(attachment.kind),
        tg_msg_id=int(tg_msg_id),
        tg_topic_id=tg_topic_id,
        source=source,
        media_chat_id=getattr(attachment, "media_chat_id", None) or msg.chat_id,
        media_msg_id=getattr(attachment, "media_msg_id", None) or msg.msg_id,
        reference_kind=reference_kind,
        reference_id=reference_id,
        commit=commit,
    )


async def save_delivered_job_part(
    *,
    repo: Repository,
    job: PendingMediaDownload,
    tg_msg_id: int,
    source: str,
    commit: bool = True,
) -> None:
    saver = getattr(repo, "save_delivered_media_part", None)
    if not callable(saver) or not tg_msg_id:
        return
    base_msg_id = canonical_media_base_msg_id(job.max_msg_id)
    if not base_msg_id:
        return
    await saver(
        max_chat_id=job.max_chat_id,
        base_max_msg_id=base_msg_id,
        attachment_index=int(job.attachment_index),
        kind=media_part_kind(job.kind),
        tg_msg_id=int(tg_msg_id),
        tg_topic_id=job.tg_topic_id,
        source=source,
        media_chat_id=job.media_chat_id,
        media_msg_id=job.media_msg_id,
        reference_kind=job.reference_kind if job.reference_id else None,
        reference_id=job.reference_id or None,
        commit=commit,
    )


async def is_job_part_delivered(
    *,
    repo: Repository,
    job: PendingMediaDownload,
):
    base_msg_id = canonical_media_base_msg_id(job.max_msg_id)
    if not base_msg_id:
        return None
    return await find_delivered_media_part(
        repo=repo,
        max_chat_id=job.max_chat_id,
        base_max_msg_id=base_msg_id,
        attachment_index=int(job.attachment_index),
        kind=job.kind,
        reference_kind=job.reference_kind if job.reference_id else None,
        reference_id=job.reference_id or None,
    )


async def is_edit_media_resolved_by_base_delivery(*, repo: Repository, msg) -> bool:
    base_msg_id = edit_base_max_msg_id(msg.msg_id, getattr(msg, "status", None))
    if not base_msg_id or base_msg_id == msg.msg_id:
        return False
    if await has_any_delivered_media_parts(
        repo=repo,
        max_chat_id=msg.chat_id,
        base_max_msg_id=base_msg_id,
    ):
        return False
    latest_delivery = await repo.get_latest_delivery(msg.chat_id, base_msg_id, "inbound")
    return is_late_duplicate_resolved_delivery(latest_delivery)


async def mark_pending_media_delivered_if_late_recovered(
    *,
    repo: Repository,
    job: PendingMediaDownload,
    flow_id: str,
) -> bool:
    if not job.id:
        return False
    delivered_part = await is_job_part_delivered(repo=repo, job=job)
    if delivered_part is not None:
        await repo.mark_pending_media_delivered(
            job.id,
            tg_msg_id=int(getattr(delivered_part, "tg_msg_id", 0) or 0),
        )
        log_event(
            logger,
            logging.INFO,
            "bridge.media_retry.resolved",
            flow_id=flow_id,
            direction="inbound",
            stage="media_retry",
            outcome="delivered",
            reason="media_part_already_delivered",
            max_chat_id=job.max_chat_id,
            max_msg_id=job.max_msg_id,
            tg_topic_id=job.tg_topic_id,
            pending_media_id=job.id,
            attachment_index=job.attachment_index,
            kind=job.kind,
        )
        return True
    base_msg_id = canonical_media_base_msg_id(job.max_msg_id)
    if base_msg_id and await has_any_delivered_media_parts(
        repo=repo,
        max_chat_id=job.max_chat_id,
        base_max_msg_id=base_msg_id,
    ):
        return False
    latest_delivery = await repo.get_latest_delivery(job.max_chat_id, job.max_msg_id, "inbound")
    if not is_late_duplicate_resolved_delivery(latest_delivery):
        base_msg_id = edit_base_max_msg_id(job.max_msg_id)
        if base_msg_id:
            latest_delivery = await repo.get_latest_delivery(job.max_chat_id, base_msg_id, "inbound")
    if not is_late_duplicate_resolved_delivery(latest_delivery):
        return False
    await repo.mark_pending_media_delivered(job.id, tg_msg_id=0)
    log_event(
        logger,
        logging.INFO,
        "bridge.media_retry.resolved",
        flow_id=flow_id,
        direction="inbound",
        stage="media_retry",
        outcome="delivered",
        reason="late_duplicate_media_already_delivered",
        max_chat_id=job.max_chat_id,
        max_msg_id=job.max_msg_id,
        tg_topic_id=job.tg_topic_id,
        pending_media_id=job.id,
        attachment_index=job.attachment_index,
        kind=job.kind,
    )
    return True


def pending_media_retry_delay(attempts_after_failure: int) -> int:
    # Бесконечный retry с cap: 1m, 2m, 4m ... до 6h.
    return exponential_backoff_seconds(
        attempts_after_failure,
        base_seconds=60,
        cap_seconds=6 * 3600,
        max_exponent=8,
    )


def media_source_pair(
    *,
    source_chat_id: object,
    source_msg_id: object,
    fallback_chat_id: object,
    fallback_msg_id: object,
) -> tuple[str, str, bool]:
    if is_usable_max_chat_id(source_chat_id):
        return str(source_chat_id), str(source_msg_id or fallback_msg_id), False
    return str(fallback_chat_id), str(source_msg_id or fallback_msg_id), True


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
    legacy_edit_media_resolved = await is_edit_media_resolved_by_base_delivery(repo=repo, msg=msg)
    for failure in msg.attachment_failures:
        media_part_delivered = await is_failure_delivered(
            repo=repo,
            msg=msg,
            failure=failure,
        )
        if media_part_delivered or legacy_edit_media_resolved:
            log_event(
                logger,
                logging.INFO,
                "bridge.media_retry.suppressed",
                flow_id=flow_id,
                direction="inbound",
                stage="media_retry",
                outcome="skipped",
                reason=(
                    "media_part_already_delivered"
                    if media_part_delivered
                    else "edit_base_media_already_delivered"
                ),
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=topic_id,
                attachment_index=failure.index,
                kind=failure.kind,
            )
            continue
        if not is_retryable_media_failure(failure):
            existing = await find_existing_pending_media_for_failure(repo=repo, msg=msg, failure=failure)
            if existing is not None:
                log_event(
                    logger,
                    logging.INFO,
                    "bridge.media_retry.enqueued",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="media_retry",
                    outcome="existing",
                    reason="pending_late_duplicate_finalizer_exists",
                    max_chat_id=msg.chat_id,
                    max_msg_id=msg.msg_id,
                    tg_topic_id=topic_id,
                    pending_media_id=existing.id,
                    attachment_index=failure.index,
                    kind=failure.kind,
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
                    media_chat_id=str(failure.media_chat_id or msg.chat_id),
                    media_msg_id=str(failure.media_msg_id or msg.msg_id),
                    reference_kind=LATE_DUPLICATE_REFERENCE_KIND,
                    reference_id="",
                    filename=failure.filename,
                    duration=failure.duration,
                    width=failure.width,
                    height=failure.height,
                    next_attempt_at=now + LATE_DUPLICATE_FINAL_DELAY_SECONDS,
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
                reason="late_duplicate_finalizer",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=topic_id,
                pending_media_id=job_id,
                attachment_index=failure.index,
                kind=failure.kind,
                retry_in_seconds=LATE_DUPLICATE_FINAL_DELAY_SECONDS,
            )
            continue

        media_chat_id, media_msg_id, source_fallback = media_source_pair(
            source_chat_id=failure.media_chat_id,
            source_msg_id=failure.media_msg_id,
            fallback_chat_id=msg.chat_id,
            fallback_msg_id=msg.msg_id,
        )

        existing = await find_existing_pending_media_for_failure(
            repo=repo,
            msg=msg,
            failure=failure,
            media_chat_id=media_chat_id,
            media_msg_id=media_msg_id,
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
                media_source_fallback=source_fallback,
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
                media_chat_id=media_chat_id,
                media_msg_id=media_msg_id,
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
            media_source_fallback=source_fallback,
        )
    return enqueued, display_failures


async def find_existing_pending_media_for_failure(
    *,
    repo: Repository,
    msg,
    failure: MaxAttachmentFailure,
    media_chat_id: str | None = None,
    media_msg_id: str | None = None,
) -> PendingMediaDownload | None:
    finder = getattr(repo, "find_active_pending_media", None)
    if callable(finder):
        candidate_ids = [str(msg.msg_id)]
        base_msg_id = canonical_media_base_msg_id(
            getattr(msg, "msg_id", None),
            getattr(msg, "status", None),
        )
        if base_msg_id and base_msg_id not in candidate_ids:
            candidate_ids.append(base_msg_id)
        for candidate_id in candidate_ids:
            existing = await finder(
                max_chat_id=msg.chat_id,
                max_msg_id=candidate_id,
                attachment_index=failure.index,
                kind=media_part_kind(failure.kind),
            )
            if existing is not None:
                return existing

    ref_finder = getattr(repo, "find_active_pending_media_by_reference", None)
    if (
        callable(ref_finder)
        and failure.reference_kind
        and failure.reference_id
        and (media_chat_id or failure.media_chat_id or msg.chat_id)
        and (media_msg_id or failure.media_msg_id or msg.msg_id)
    ):
        return await ref_finder(
            media_chat_id=media_chat_id or failure.media_chat_id or msg.chat_id,
            media_msg_id=media_msg_id or failure.media_msg_id or msg.msg_id,
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
    if await mark_pending_media_delivered_if_late_recovered(
        repo=repo,
        job=job,
        flow_id=flow_id,
    ):
        return
    if is_late_duplicate_finalizer_job(job):
        await tg.send_text(
            job.tg_topic_id,
            compose_terminal_media_failure_text(
                kind=job.kind,
                index=job.attachment_index,
                source_type=job.source_type,
            ),
            flow_id=flow_id,
        )
        await repo.mark_pending_media_failed(job.id, error="late_media_not_recovered")
        log_event(
            logger,
            logging.ERROR,
            "bridge.media_retry.failed",
            flow_id=flow_id,
            direction="inbound",
            stage="media_retry",
            outcome="failed",
            reason="late_media_not_recovered",
            max_chat_id=job.max_chat_id,
            max_msg_id=job.max_msg_id,
            tg_topic_id=job.tg_topic_id,
            pending_media_id=job.id,
            attachment_index=job.attachment_index,
            kind=job.kind,
        )
        return
    if not job.reference_id or not (
        (job.kind == "video" and job.reference_kind == "video_id")
        or (job.kind == "audio" and job.reference_kind in {"audio_id", "file_id"})
    ):
        await tg.send_text(
            job.tg_topic_id,
            compose_terminal_media_failure_text(
                kind=job.kind,
                index=job.attachment_index,
                source_type=job.source_type,
            ),
            flow_id=flow_id,
        )
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

    media_chat_id, media_msg_id, source_fallback = media_source_pair(
        source_chat_id=job.media_chat_id,
        source_msg_id=job.media_msg_id,
        fallback_chat_id=job.max_chat_id,
        fallback_msg_id=job.max_msg_id,
    )

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
        media_source_fallback=source_fallback,
    )

    if job.kind == "audio":
        download_media = max_adapter.download_audio_reference
    else:
        download_media = max_adapter.download_video_reference

    try:
        if job.kind == "audio":
            attachment = await download_media(
                chat_id=media_chat_id,
                msg_id=media_msg_id,
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
                chat_id=media_chat_id,
                msg_id=media_msg_id,
                video_id=job.reference_id,
                attachment_index=job.attachment_index,
                filename_hint=job.filename,
                duration=job.duration,
                width=job.width,
                height=job.height,
                source_type=job.source_type or "VIDEO",
                flow_id=flow_id,
            )
            if attachment is None and source_fallback and media_msg_id != str(job.max_msg_id):
                log_event(
                    logger,
                    logging.INFO,
                    "bridge.media_retry.source_fallback",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="media_retry",
                    outcome="retry",
                    reason="fallback_to_wrapper_message_id",
                    max_chat_id=job.max_chat_id,
                    max_msg_id=job.max_msg_id,
                    tg_topic_id=job.tg_topic_id,
                    pending_media_id=job.id,
                    attachment_index=job.attachment_index,
                    kind=job.kind,
                )
                attachment = await download_media(
                    chat_id=job.max_chat_id,
                    msg_id=job.max_msg_id,
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
        if await mark_pending_media_delivered_if_late_recovered(
            repo=repo,
            job=job,
            flow_id=flow_id,
        ):
            return
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
        await save_delivered_job_part(
            repo=repo,
            job=job,
            tg_msg_id=tg_msg_id,
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
