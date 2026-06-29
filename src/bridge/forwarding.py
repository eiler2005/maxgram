"""MAX to Telegram forwarding helpers."""

import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Optional

from . import actions as bridge_actions
from . import mapping
from . import inbound_retry
from . import media_retry
from .contracts import (
    MaxAttachment,
    MaxAttachmentFailure,
    MaxMessage,
    TelegramBridgePort,
    is_probable_client_cid,
)
from ..config.loader import AppConfig
from ..db.repository import Repository
from ..logging_utils import build_max_flow_id, log_event

logger = logging.getLogger("src.bridge.core")


_LATE_MEDIA_KIND_LABELS = {
    "photo": "фото",
    "video": "видео",
    "audio": "аудио",
    "document": "файл",
}


def compose_message_text(primary: str, secondary: str = "") -> str:
    parts = [part.strip() for part in [primary, secondary] if part and part.strip()]
    return "\n".join(parts)


def compose_attachment_failure_text(failures: list[MaxAttachmentFailure]) -> str:
    lines = []
    for failure in failures:
        lines.append(media_retry.compose_pending_media_text(failure))
    return "\n".join(lines)


def format_duration_compact(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}с"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}м"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}ч"
    return f"{hours // 24}д"


def is_file_too_large(cfg: AppConfig, path: str) -> bool:
    max_size_mb = cfg.bridge.max_file_size_mb
    if max_size_mb <= 0:
        return False
    try:
        return Path(path).stat().st_size > max_size_mb * 1024 * 1024
    except OSError:
        return False


async def send_attachment(
    *,
    cfg: AppConfig,
    tg: TelegramBridgePort,
    topic_id: int,
    attachment: MaxAttachment,
    caption: str,
    flow_id: Optional[str] = None,
) -> Optional[int]:
    """Отправить одно вложение в Telegram."""
    if attachment.kind == "photo":
        return await tg.send_photo(topic_id, attachment.local_path, caption, flow_id=flow_id)

    if attachment.kind == "document":
        return await tg.send_document(
            topic_id, attachment.local_path, caption, attachment.filename or "", flow_id=flow_id
        )

    if attachment.kind == "video":
        return await tg.send_video(
            topic_id,
            attachment.local_path,
            caption,
            attachment.filename or "",
            duration=attachment.duration,
            width=attachment.width,
            height=attachment.height,
            flow_id=flow_id,
        )

    if attachment.kind == "audio":
        source_type = str(attachment.source_type or "").upper()
        if "VOICE" in source_type or "AUDIO" in source_type:
            if not getattr(cfg.content, "forward_voice", True):
                log_event(
                    logger,
                    logging.INFO,
                    "bridge.inbound.media_skipped",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="forward",
                    outcome="skipped",
                    reason="forward_voice_disabled",
                    media_type="voice",
                    source_type=source_type,
                )
                placeholder = cfg.content.placeholder_unsupported.format(
                    type=attachment.source_type or "voice"
                )
                return await tg.send_text(
                    topic_id,
                    compose_message_text(caption, placeholder),
                    flow_id=flow_id,
                )

            sent_id = await tg.send_voice(
                topic_id,
                attachment.local_path,
                caption,
                duration=attachment.duration,
                flow_id=flow_id,
            )
            if sent_id:
                return sent_id

            log_event(
                logger,
                logging.WARNING,
                "bridge.inbound.voice_fallback",
                flow_id=flow_id,
                direction="inbound",
                stage="forward",
                outcome="retry",
                reason="send_voice_failed",
                media_type="voice",
                source_type=source_type,
            )
            return await tg.send_audio(
                topic_id,
                attachment.local_path,
                caption,
                attachment.filename or "",
                duration=attachment.duration,
                flow_id=flow_id,
            )
        return await tg.send_audio(
            topic_id,
            attachment.local_path,
            caption,
            attachment.filename or "",
            duration=attachment.duration,
            flow_id=flow_id,
        )

    placeholder = cfg.content.placeholder_unsupported.format(
        type=attachment.source_type or attachment.kind
    )
    return await tg.send_text(
        topic_id,
        compose_message_text(caption, placeholder),
        flow_id=flow_id,
    )


def _is_late_media_recovery_candidate(latest_delivery: Optional[dict]) -> bool:
    if not latest_delivery:
        return False
    if latest_delivery.get("status") != "partial":
        return False
    error = str(latest_delivery.get("error") or "")
    return error.startswith("attachment_download_failed:")


def _late_media_caption(attachment: MaxAttachment, index: int) -> str:
    label = _LATE_MEDIA_KIND_LABELS.get(attachment.kind, attachment.kind or "медиа")
    return f"Досланное {label} MAX #{index + 1}"


async def recover_late_duplicate_media(
    *,
    cfg: AppConfig,
    tg: TelegramBridgePort,
    repo: Repository,
    msg: MaxMessage,
    get_or_create_topic: Callable[..., Awaitable[Optional[int]]],
    flow_id: str,
) -> bool:
    if not msg.attachments:
        return False

    latest_delivery = await repo.get_latest_delivery(msg.chat_id, msg.msg_id, "inbound")
    undelivered = await media_retry.undelivered_attachments(
        repo=repo,
        msg=msg,
        attachments=msg.attachments,
    )
    if not undelivered:
        return False
    if not _is_late_media_recovery_candidate(latest_delivery):
        has_pending_part = False
        for part_index, attachment in undelivered:
            if await media_retry.has_active_pending_media_part(
                repo=repo,
                msg=msg,
                attachment_index=part_index,
                kind=attachment.kind,
            ):
                has_pending_part = True
                break
        if not has_pending_part:
            return False

    binding = await repo.get_binding(msg.chat_id)
    if binding and binding.mode == "disabled":
        return False
    topic_id = binding.tg_topic_id if binding else await get_or_create_topic(msg, flow_id=flow_id)
    if topic_id is None:
        return False

    sent_records: list[tuple[int, MaxAttachment, int]] = []
    for index, attachment in undelivered:
        attachment_path = Path(attachment.local_path)
        if not attachment_path.exists():
            continue
        caption = _late_media_caption(attachment, index)
        if is_file_too_large(cfg, attachment.local_path):
            placeholder = cfg.content.placeholder_file_too_large.format(
                filename=attachment.filename or attachment_path.name
            )
            sent_id = await tg.send_text(
                topic_id,
                compose_message_text(caption, placeholder),
                flow_id=flow_id,
            )
        else:
            sent_id = await send_attachment(
                cfg=cfg,
                tg=tg,
                topic_id=topic_id,
                attachment=attachment,
                caption=caption,
                flow_id=flow_id,
            )
        if sent_id:
            sent_records.append((index, attachment, sent_id))

    if len(sent_records) != len(undelivered):
        log_event(
            logger,
            logging.WARNING,
            "bridge.inbound.late_media_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="dedup",
            outcome="failed",
            reason="late_media_send_incomplete",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=topic_id,
            sent_media_count=len(sent_records),
            attachment_count=len(undelivered),
        )
        return False

    for attachment in msg.attachments:
        with suppress(Exception):
            Path(attachment.local_path).unlink(missing_ok=True)

    async with mapping.repo_transaction(repo):
        reply_max_msg_id = (
            media_retry.canonical_media_base_msg_id(msg.msg_id, msg.status)
            or msg.msg_id
        )
        for _index, _attachment, sent_id in sent_records:
            await repo.save_tg_reply_mapping(
                sent_id,
                msg.chat_id,
                reply_max_msg_id,
                topic_id,
                source="late_media_recovery",
                commit=False,
            )
            await media_retry.save_delivered_attachment_part(
                repo=repo,
                msg=msg,
                attachment=_attachment,
                tg_msg_id=sent_id,
                tg_topic_id=topic_id,
                source="late_media_recovery",
                fallback_index=_index,
                commit=False,
            )
        find_pending = getattr(repo, "find_active_pending_media", None)
        if callable(find_pending):
            for index, attachment, sent_id in sent_records:
                for candidate_id in dict.fromkeys((msg.msg_id, reply_max_msg_id)):
                    pending = await find_pending(
                        max_chat_id=msg.chat_id,
                        max_msg_id=candidate_id,
                        attachment_index=index,
                        kind=media_retry.media_part_kind(attachment.kind),
                    )
                    if pending and pending.id:
                        await repo.mark_pending_media_delivered(
                            pending.id,
                            tg_msg_id=sent_id,
                        )
        await repo.log_delivery(
            msg.msg_id,
            msg.chat_id,
            "inbound",
            "delivered",
            "late_media_recovered",
        )

    log_event(
        logger,
        logging.INFO,
        "bridge.inbound.late_media_recovery",
        flow_id=flow_id,
        direction="inbound",
        stage="dedup",
        outcome="delivered",
        reason="late_media_recovered",
        max_chat_id=msg.chat_id,
        max_msg_id=msg.msg_id,
        tg_topic_id=topic_id,
        sent_media_count=len(sent_records),
    )
    return True


async def forward_to_telegram(
    *,
    cfg: AppConfig,
    tg: TelegramBridgePort,
    repo: Repository,
    msg: MaxMessage,
    topic_id: int,
    flow_id: Optional[str] = None,
    attachment_failures: Optional[list[MaxAttachmentFailure]] = None,
) -> Optional[int]:
    """Отправить сообщение в Telegram топик. Возвращает tg_msg_id."""
    sender_prefix = ""
    if msg.is_own:
        sender_prefix = "[Вы] "
    elif not msg.is_dm and msg.sender_name:
        sender_prefix = f"[{msg.sender_name}] "

    body_text = f"{sender_prefix}{msg.text}".strip() if msg.text else ""
    media_caption = body_text or (sender_prefix.strip() if msg.attachments else "")
    extra_text = "\n".join(part for part in msg.rendered_texts if part).strip()
    prepared_buttons = await bridge_actions.prepare_telegram_buttons(
        repo=repo,
        msg=msg,
        topic_id=topic_id,
    )
    tg_msg_id = None
    emitted_anything = False

    attachments_to_send = await media_retry.undelivered_attachments(
        repo=repo,
        msg=msg,
        attachments=msg.attachments,
    )
    for attachment_index, attachment in attachments_to_send:
        attachment_path = Path(attachment.local_path)
        if not attachment_path.exists():
            continue

        delivered_media = False
        if is_file_too_large(cfg, attachment.local_path):
            placeholder = cfg.content.placeholder_file_too_large.format(
                filename=attachment.filename or attachment_path.name
            )
            text = compose_message_text("" if emitted_anything else media_caption, placeholder)
            sent_id = await tg.send_text(topic_id, text, flow_id=flow_id)
        else:
            caption = "" if emitted_anything else media_caption
            sent_id = await send_attachment(
                cfg=cfg,
                tg=tg,
                topic_id=topic_id,
                attachment=attachment,
                caption=caption,
                flow_id=flow_id,
            )
            delivered_media = attachment.kind in {"photo", "video", "audio", "document"}

        if sent_id:
            emitted_anything = True
            if tg_msg_id is None:
                tg_msg_id = sent_id
            if delivered_media:
                reply_max_msg_id = (
                    media_retry.canonical_media_base_msg_id(msg.msg_id, msg.status)
                    or msg.msg_id
                )
                await media_retry.save_delivered_attachment_part(
                    repo=repo,
                    msg=msg,
                    attachment=attachment,
                    tg_msg_id=sent_id,
                    tg_topic_id=topic_id,
                    source="media_part",
                    fallback_index=attachment_index,
                )
                await repo.save_tg_reply_mapping(
                    sent_id,
                    msg.chat_id,
                    reply_max_msg_id,
                    topic_id,
                    source="media_part",
                )

    if extra_text:
        text = compose_message_text("" if emitted_anything else body_text, extra_text)
        sent_id = await tg.send_text(
            topic_id,
            text,
            flow_id=flow_id,
            buttons=prepared_buttons.buttons or None,
        )
        await bridge_actions.attach_callback_actions_to_message(
            repo=repo,
            action_ids=prepared_buttons.callback_action_ids,
            tg_msg_id=sent_id,
        )
        if sent_id:
            emitted_anything = True
            if tg_msg_id is None:
                tg_msg_id = sent_id
    elif prepared_buttons.buttons:
        text = body_text if not emitted_anything and body_text else "Доступны действия MAX"
        sent_id = await tg.send_text(
            topic_id,
            text,
            flow_id=flow_id,
            buttons=prepared_buttons.buttons,
        )
        await bridge_actions.attach_callback_actions_to_message(
            repo=repo,
            action_ids=prepared_buttons.callback_action_ids,
            tg_msg_id=sent_id,
        )
        if sent_id:
            emitted_anything = True
            if tg_msg_id is None:
                tg_msg_id = sent_id

    failures_to_display = (
        msg.attachment_failures
        if attachment_failures is None
        else attachment_failures
    )
    failure_text = compose_attachment_failure_text(failures_to_display)
    if failure_text:
        text = compose_message_text("" if emitted_anything else body_text, failure_text)
        sent_id = await tg.send_text(topic_id, text, flow_id=flow_id)
        if sent_id:
            emitted_anything = True
            if tg_msg_id is None:
                tg_msg_id = sent_id

    if not emitted_anything and body_text:
        tg_msg_id = await tg.send_text(topic_id, body_text, flow_id=flow_id)

    elif not emitted_anything:
        cfg_content = cfg.content
        media_type = next((atype.lower() for atype in msg.attachment_types if atype), "unknown")
        placeholder = cfg_content.placeholder_unsupported.format(type=media_type)
        tg_msg_id = await tg.send_text(
            topic_id,
            compose_message_text(body_text, placeholder),
            flow_id=flow_id,
        )

    for attachment in msg.attachments:
        with suppress(Exception):
            Path(attachment.local_path).unlink(missing_ok=True)

    return tg_msg_id


async def handle_max_message(
    *,
    cfg: AppConfig,
    tg: TelegramBridgePort,
    repo: Repository,
    stats: dict[str, int | float],
    msg: MaxMessage,
    get_or_create_topic: Callable[..., Awaitable[Optional[int]]],
    message_has_control_event: Callable[[MaxMessage], bool],
    schedule_recovery_event_scan: Callable[[str], None],
    enqueue_retryable_media_failures: Callable[..., Awaitable[tuple[int, list[MaxAttachmentFailure]]]],
    forward_to_telegram_fn: Callable[..., Awaitable[Optional[int]]],
    get_last_tg_send_error: Callable[[], Optional[str]],
):
    """Route one inbound MAX message into Telegram."""
    flow_id = build_max_flow_id(msg.chat_id, msg.msg_id)

    if not msg.msg_id or not msg.chat_id:
        return

    if is_probable_client_cid(msg.chat_id):
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound.forward_finished",
            flow_id=flow_id,
            direction="inbound",
            stage="routing",
            outcome="skipped",
            reason="probable_client_cid_chat_id",
            max_msg_id=msg.msg_id,
            message_type=msg.message_type,
            attachment_types=msg.attachment_types,
        )
        return

    if msg.is_own:
        if await repo.is_duplicate(msg.msg_id, msg.chat_id):
            if await recover_late_duplicate_media(
                cfg=cfg,
                tg=tg,
                repo=repo,
                msg=msg,
                get_or_create_topic=get_or_create_topic,
                flow_id=flow_id,
            ):
                stats["inbound_media"] += 1
                return
            log_event(
                logger,
                logging.INFO,
                "bridge.inbound.dedup",
                flow_id=flow_id,
                direction="inbound",
                stage="dedup",
                outcome="skipped",
                reason="duplicate",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
            )
            return
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound.dedup",
            flow_id=flow_id,
            direction="inbound",
            stage="dedup",
            outcome="accepted",
            reason="own_direct_message",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
        )

    elif await repo.is_duplicate(msg.msg_id, msg.chat_id):
        if await recover_late_duplicate_media(
            cfg=cfg,
            tg=tg,
            repo=repo,
            msg=msg,
            get_or_create_topic=get_or_create_topic,
            flow_id=flow_id,
        ):
            stats["inbound_media"] += 1
            return
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound.dedup",
            flow_id=flow_id,
            direction="inbound",
            stage="dedup",
            outcome="skipped",
            reason="duplicate",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
        )
        return
    else:
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound.dedup",
            flow_id=flow_id,
            direction="inbound",
            stage="dedup",
            outcome="accepted",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
        )

    if msg.sender_id and msg.sender_name and not msg.is_own:
        await repo.save_user(msg.sender_id, msg.sender_name)

    await mapping.save_inbound_idempotency_key(repo, msg)

    topic_id = await get_or_create_topic(msg, flow_id=flow_id)
    if topic_id is None:
        log_event(
            logger,
            logging.ERROR,
            "bridge.inbound.forward_finished",
            flow_id=flow_id,
            direction="inbound",
            stage="routing",
            outcome="failed",
            reason="no_topic",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
        )
        await repo.log_delivery(msg.msg_id, msg.chat_id, "inbound", "failed", "no_topic")
        return
    if message_has_control_event(msg):
        schedule_recovery_event_scan("control_event")

    binding = await repo.get_binding(msg.chat_id)
    if binding and binding.mode == "disabled":
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound.forward_finished",
            flow_id=flow_id,
            direction="inbound",
            stage="routing",
            outcome="skipped",
            reason="disabled",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=topic_id,
        )
        return

    log_event(
        logger,
        logging.INFO,
        "bridge.inbound.forward_started",
        flow_id=flow_id,
        direction="inbound",
        stage="forward",
        outcome="started",
        max_chat_id=msg.chat_id,
        max_msg_id=msg.msg_id,
        tg_topic_id=topic_id,
    )
    enqueued_media = 0
    display_failures = msg.attachment_failures
    if msg.attachment_failures:
        enqueued_media, display_failures = await enqueue_retryable_media_failures(
            msg,
            topic_id,
            flow_id=flow_id,
        )
    attachment_failures_resolved = (
        bool(msg.attachment_failures)
        and not display_failures
        and await media_retry.are_failures_delivered_or_legacy_resolved(
            repo=repo,
            msg=msg,
            failures=msg.attachment_failures,
        )
    )

    if (
        not msg.text
        and not msg.attachments
        and not msg.rendered_texts
        and not msg.actions
        and msg.attachment_failures
        and not display_failures
    ):
        if attachment_failures_resolved:
            await repo.log_delivery(
                msg.msg_id,
                msg.chat_id,
                "inbound",
                "delivered",
                "media_parts_already_delivered",
            )
            log_event(
                logger,
                logging.INFO,
                "bridge.inbound.forward_finished",
                flow_id=flow_id,
                direction="inbound",
                stage="forward",
                outcome="delivered",
                reason="media_parts_already_delivered",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=topic_id,
                failed_attachment_count=len(msg.attachment_failures),
                enqueued_media_count=enqueued_media,
            )
            return
        await repo.log_delivery(
            msg.msg_id,
            msg.chat_id,
            "inbound",
            "partial",
            "attachment_download_pending_duplicate",
        )
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound.forward_finished",
            flow_id=flow_id,
            direction="inbound",
            stage="forward",
            outcome="skipped",
            reason="duplicate_pending_media",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=topic_id,
            failed_attachment_count=len(msg.attachment_failures),
            enqueued_media_count=enqueued_media,
        )
        return

    all_attachments_already_delivered = False
    if (
        msg.attachments
        and not msg.text
        and not msg.rendered_texts
        and not msg.actions
        and not msg.attachment_failures
    ):
        all_attachments_already_delivered = not await media_retry.undelivered_attachments(
            repo=repo,
            msg=msg,
            attachments=msg.attachments,
        )

    if all_attachments_already_delivered:
        await repo.log_delivery(
            msg.msg_id,
            msg.chat_id,
            "inbound",
            "delivered",
            "media_parts_already_delivered",
        )
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound.forward_finished",
            flow_id=flow_id,
            direction="inbound",
            stage="forward",
            outcome="delivered",
            reason="media_parts_already_delivered",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=topic_id,
        )
        return

    tg_msg_id = await forward_to_telegram_fn(
        msg,
        topic_id,
        flow_id=flow_id,
        attachment_failures=display_failures,
    )

    if tg_msg_id:
        if msg.attachment_failures and not attachment_failures_resolved:
            delivery_status = "partial"
            delivery_error = f"attachment_download_failed:{len(msg.attachment_failures)}"
            log_level = logging.WARNING
        else:
            delivery_status = "delivered"
            delivery_error = None
            log_level = logging.INFO

        async with mapping.repo_transaction(repo):
            await mapping.save_inbound_delivery_mapping(
                repo,
                msg,
                tg_msg_id=tg_msg_id,
                tg_topic_id=topic_id,
            )
            await repo.log_delivery(
                msg.msg_id,
                msg.chat_id,
                "inbound",
                delivery_status,
                delivery_error,
            )
        if msg.attachments or msg.attachment_failures:
            stats["inbound_media"] += 1
        else:
            stats["inbound_text"] += 1
        log_event(
            logger,
            log_level,
            "bridge.inbound.forward_finished",
            flow_id=flow_id,
            direction="inbound",
            stage="forward",
            outcome=delivery_status,
            reason=delivery_error,
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=topic_id,
            tg_msg_id=tg_msg_id,
            failed_attachment_count=len(msg.attachment_failures),
            enqueued_media_count=enqueued_media,
        )
    else:
        tg_error = get_last_tg_send_error() or "tg_send_failed"
        if (
            inbound_retry.is_text_only_inbound_retry_candidate(msg)
            and inbound_retry.is_retryable_tg_delivery_error(tg_error)
        ):
            async with mapping.repo_transaction(repo):
                pending_id = await inbound_retry.enqueue_text_inbound_retry(
                    repo=repo,
                    msg=msg,
                    topic_id=topic_id,
                    error=tg_error,
                    attempts=1,
                )
                await repo.log_delivery(
                    msg.msg_id,
                    msg.chat_id,
                    "inbound",
                    "pending",
                    "tg_send_queued",
                )
            log_event(
                logger,
                logging.WARNING,
                "bridge.inbound.forward_finished",
                flow_id=flow_id,
                direction="inbound",
                stage="forward",
                outcome="queued",
                reason="tg_send_queued",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=topic_id,
                pending_inbound_id=pending_id,
                error=tg_error,
            )
            return

        await repo.log_delivery(
            msg.msg_id,
            msg.chat_id,
            "inbound",
            "failed",
            tg_error,
        )
        stats["failed_inbound"] += 1
        log_event(
            logger,
            logging.ERROR,
            "bridge.inbound.forward_finished",
            flow_id=flow_id,
            direction="inbound",
            stage="forward",
            outcome="failed",
            reason="tg_send_failed",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=topic_id,
            error=tg_error,
        )
