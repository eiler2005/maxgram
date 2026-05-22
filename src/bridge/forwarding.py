"""MAX to Telegram forwarding helpers."""

import logging
from pathlib import Path
from typing import Optional

from . import media_retry
from .contracts import MaxAttachment, MaxAttachmentFailure, MaxMessage, TelegramBridgePort
from ..config.loader import AppConfig
from ..logging_utils import log_event

logger = logging.getLogger("src.bridge.core")


def compose_message_text(primary: str, secondary: str = "") -> str:
    parts = [part.strip() for part in [primary, secondary] if part and part.strip()]
    return "\n".join(parts)


def compose_attachment_failure_text(failures: list[MaxAttachmentFailure]) -> str:
    lines = []
    for failure in failures:
        label = failure.filename or f"{failure.kind} #{failure.index + 1}"
        if media_retry.is_retryable_media_failure(failure):
            media_label = "Голосовое MAX" if failure.kind == "audio" else "Видео MAX"
            lines.append(
                f"⏳ {media_label} #{failure.index + 1} докачивается "
                "и будет дослано позже"
            )
        else:
            lines.append(f"⚠️ Не удалось скачать вложение MAX: {label}")
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


async def forward_to_telegram(
    *,
    cfg: AppConfig,
    tg: TelegramBridgePort,
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
    tg_msg_id = None
    emitted_anything = False

    for attachment in msg.attachments:
        attachment_path = Path(attachment.local_path)
        if not attachment_path.exists():
            continue

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

        if sent_id:
            emitted_anything = True
            if tg_msg_id is None:
                tg_msg_id = sent_id

    if extra_text:
        text = compose_message_text("" if emitted_anything else body_text, extra_text)
        sent_id = await tg.send_text(topic_id, text, flow_id=flow_id)
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
        try:
            Path(attachment.local_path).unlink(missing_ok=True)
        except Exception:
            pass

    return tg_msg_id
