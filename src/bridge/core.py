"""
Bridge Core — центральная логика роутинга.

MAX message → Telegram topic
Telegram reply → MAX message

Принципы:
  - Все решения здесь, адаптеры только транспорт
  - Deduplication по max_msg_id
  - Auto-create топик при первом сообщении из нового чата
  - DM чаты: резолвим имя через MAX API
  - Не хранить содержимое сообщений в логах
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from ..adapters.max_adapter import MaxAdapter, MaxAttachment, MaxMessage
from ..adapters.tg_adapter import TelegramAdapter
from ..config.loader import AppConfig
from ..db.repository import Repository, ChatBinding, MessageRecord

logger = logging.getLogger(__name__)


class BridgeCore:
    def __init__(self, config: AppConfig, repo: Repository,
                 max_adapter: MaxAdapter, tg_adapter: TelegramAdapter):
        self._cfg = config
        self._repo = repo
        self._max = max_adapter
        self._tg = tg_adapter

        # Регистрируем обработчики
        self._max.on_message(self._on_max_message)
        self._tg.on_reply(self._on_tg_reply)

    # ── MAX → Telegram ────────────────────────────────────────────────────

    async def _on_max_message(self, msg: MaxMessage):
        """Входящее сообщение из MAX → форвардим в Telegram."""

        if not msg.msg_id or not msg.chat_id:
            return

        # Собственные сообщения: фильтруем эхо bridge-отправок, остальные форвардим
        if msg.is_own:
            if await self._repo.is_duplicate(msg.msg_id, msg.chat_id):
                logger.debug("Own message echo skipped msg_id=%s", msg.msg_id)
                return
            # Прямое сообщение из MAX (не через bridge) — форвардим с пометкой
            logger.debug("Own direct MAX message msg_id=%s — forwarding to TG", msg.msg_id)
            # fall through к основному потоку

        # Дедупликация (для чужих сообщений)
        elif await self._repo.is_duplicate(msg.msg_id, msg.chat_id):
            logger.debug("Duplicate skipped msg_id=%s", msg.msg_id)
            return

        # Сохраняем сразу (idempotency key)
        await self._repo.save_message(MessageRecord(
            max_msg_id=msg.msg_id,
            max_chat_id=msg.chat_id,
            tg_msg_id=None,
            tg_topic_id=None,
            direction="inbound",
            created_at=int(time.time()),
        ))

        # Получаем или создаём топик
        topic_id = await self._get_or_create_topic(msg)
        if topic_id is None:
            logger.error("Could not get/create topic for chat_id=%s", msg.chat_id)
            await self._repo.log_delivery(msg.msg_id, msg.chat_id, "inbound", "failed",
                                          "no_topic")
            return

        # Проверяем режим чата
        binding = await self._repo.get_binding(msg.chat_id)
        if binding and binding.mode == "disabled":
            return

        # Форвардим в Telegram
        tg_msg_id = await self._forward_to_telegram(msg, topic_id)

        # Обновляем запись с tg_msg_id
        if tg_msg_id:
            await self._repo.save_message(MessageRecord(
                max_msg_id=msg.msg_id,
                max_chat_id=msg.chat_id,
                tg_msg_id=tg_msg_id,
                tg_topic_id=topic_id,
                direction="inbound",
                created_at=int(time.time()),
            ))
            await self._repo.log_delivery(msg.msg_id, msg.chat_id, "inbound", "delivered")
        else:
            await self._repo.log_delivery(msg.msg_id, msg.chat_id, "inbound", "failed",
                                          "tg_send_failed")

    async def _get_or_create_topic(self, msg: MaxMessage) -> Optional[int]:
        """Вернуть существующий topic_id или создать новый.
        Если топик уже есть, но имеет fallback-название — пробуем переименовать.
        """
        binding = await self._repo.get_binding(msg.chat_id)
        if binding:
            # Если название — fallback (ещё не знали имя), пробуем обновить
            if binding.title.startswith("Чат "):
                real_title = await self._resolve_chat_title(msg)
                if not real_title.startswith("Чат "):
                    await self._tg.rename_topic(binding.tg_topic_id, real_title)
                    await self._repo.update_title(msg.chat_id, real_title)
                    logger.info("Topic renamed chat_id=%s %r → %r", msg.chat_id, binding.title, real_title)
            return binding.tg_topic_id

        # Определяем название топика
        title = await self._resolve_chat_title(msg)

        # Создаём топик в Telegram
        try:
            topic_id = await self._tg.create_topic(title)
        except Exception as e:
            logger.error("create_topic failed title=%r: %s", title, e)
            return None

        # Сохраняем binding
        mode = self._cfg.get_chat_mode(msg.chat_id)
        await self._repo.save_binding(ChatBinding(
            max_chat_id=msg.chat_id,
            tg_topic_id=topic_id,
            title=title,
            mode=mode,
            created_at=int(time.time()),
        ))
        logger.info("New topic created chat_id=%s title=%r topic_id=%s", msg.chat_id, title, topic_id)
        return topic_id

    async def _resolve_chat_title(self, msg: MaxMessage) -> str:
        """Определить название для топика."""
        # 1. Из конфига
        config_title = self._cfg.get_chat_title(msg.chat_id)
        if config_title:
            return config_title

        # 2. Из сообщения (группы обычно имеют chat_title)
        if msg.chat_title:
            return msg.chat_title

        # 3. DM: резолвим имя собеседника через MAX API
        #    В DM chat_id == user_id собеседника (не нашего аккаунта!)
        #    Пробуем chat_id первым, потом sender_id (если это не наш аккаунт)
        if msg.is_dm:
            other_id = msg.chat_id  # собеседник всегда = chat_id для DM
            sender_is_other = msg.sender_id and msg.sender_id != other_id
            candidates = list(dict.fromkeys(filter(None, [
                other_id,
                msg.sender_id if sender_is_other else None,
            ])))
            for uid in candidates:
                name = await self._max.resolve_user_name(uid)
                if name:
                    return name

        # 4. Fallback
        return f"Чат {msg.chat_id}"

    def _compose_message_text(self, primary: str, secondary: str = "") -> str:
        parts = [part.strip() for part in [primary, secondary] if part and part.strip()]
        return "\n".join(parts)

    def _is_file_too_large(self, path: str) -> bool:
        max_size_mb = self._cfg.bridge.max_file_size_mb
        if max_size_mb <= 0:
            return False
        try:
            return Path(path).stat().st_size > max_size_mb * 1024 * 1024
        except OSError:
            return False

    async def _send_attachment(self, topic_id: int, attachment: MaxAttachment,
                               caption: str) -> Optional[int]:
        """Отправить одно вложение в Telegram."""
        if attachment.kind == "photo":
            return await self._tg.send_photo(topic_id, attachment.local_path, caption)

        if attachment.kind == "document":
            return await self._tg.send_document(
                topic_id, attachment.local_path, caption, attachment.filename or ""
            )

        if attachment.kind == "video":
            return await self._tg.send_video(
                topic_id,
                attachment.local_path,
                caption,
                attachment.filename or "",
                duration=attachment.duration,
                width=attachment.width,
                height=attachment.height,
            )

        if attachment.kind == "audio":
            return await self._tg.send_audio(
                topic_id,
                attachment.local_path,
                caption,
                attachment.filename or "",
                duration=attachment.duration,
            )

        placeholder = self._cfg.content.placeholder_unsupported.format(
            type=attachment.source_type or attachment.kind
        )
        return await self._tg.send_text(topic_id, self._compose_message_text(caption, placeholder))

    async def _forward_to_telegram(self, msg: MaxMessage, topic_id: int) -> Optional[int]:
        """Отправить сообщение в Telegram топик. Возвращает tg_msg_id."""

        # Формируем заголовок отправителя
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

            if self._is_file_too_large(attachment.local_path):
                placeholder = self._cfg.content.placeholder_file_too_large.format(
                    filename=attachment.filename or attachment_path.name
                )
                text = self._compose_message_text("" if emitted_anything else media_caption, placeholder)
                sent_id = await self._tg.send_text(topic_id, text)
            else:
                caption = "" if emitted_anything else media_caption
                sent_id = await self._send_attachment(topic_id, attachment, caption)

            if sent_id:
                emitted_anything = True
                if tg_msg_id is None:
                    tg_msg_id = sent_id

        if extra_text:
            text = self._compose_message_text("" if emitted_anything else body_text, extra_text)
            sent_id = await self._tg.send_text(topic_id, text)
            if sent_id:
                emitted_anything = True
                if tg_msg_id is None:
                    tg_msg_id = sent_id

        if not emitted_anything and body_text:
            tg_msg_id = await self._tg.send_text(topic_id, body_text)

        elif not emitted_anything:
            cfg = self._cfg.content
            media_type = next((atype.lower() for atype in msg.attachment_types if atype), "unknown")
            placeholder = cfg.placeholder_unsupported.format(type=media_type)
            tg_msg_id = await self._tg.send_text(
                topic_id,
                self._compose_message_text(body_text, placeholder),
            )

        # Удаляем временный файл после отправки (TTL-политика)
        for attachment in msg.attachments:
            try:
                Path(attachment.local_path).unlink(missing_ok=True)
            except Exception:
                pass

        return tg_msg_id

    # ── Telegram → MAX ────────────────────────────────────────────────────

    def _compose_tg_outbound_text(self, text: str, sender_name: Optional[str]) -> str:
        clean_text = text.strip()
        if not sender_name:
            return clean_text
        return f"[{sender_name}]\n{clean_text}" if clean_text else f"[{sender_name}]"

    async def _on_tg_reply(self, topic_id: int, text: str,
                           reply_to_tg_msg_id: Optional[int],
                           sender_name: Optional[str]):
        """Reply из Telegram → отправляем в MAX."""

        binding = await self._repo.get_binding_by_topic(topic_id)
        if not binding:
            await self._tg.send_notification(f"⚠️ Не найден MAX чат для топика {topic_id}")
            return

        if binding.mode == "readonly":
            await self._tg.send_text(
                topic_id,
                "🚫 Этот чат настроен как readonly — ответы не отправляются в MAX"
            )
            return

        if binding.mode == "disabled":
            return

        # Найти max_msg_id для reply (если есть)
        reply_to_max_id = None
        if reply_to_tg_msg_id:
            reply_to_max_id = await self._repo.get_max_msg_id_by_tg(reply_to_tg_msg_id)

        outbound_text = self._compose_tg_outbound_text(text, sender_name)
        sent_id = await self._max.send_message(
            chat_id=binding.max_chat_id,
            text=outbound_text,
            reply_to_msg_id=reply_to_max_id,
        )

        if sent_id is None:
            await self._tg.send_text(topic_id, "❌ Не удалось отправить сообщение в MAX")
            return

        await self._repo.save_message(MessageRecord(
            max_msg_id=sent_id,
            max_chat_id=binding.max_chat_id,
            tg_msg_id=None,
            tg_topic_id=topic_id,
            direction="outbound",
            created_at=int(time.time()),
        ))
        await self._repo.log_delivery(sent_id, binding.max_chat_id, "outbound", "delivered")
        logger.info("Outbound sent chat_id=%s max_msg_id=%s", binding.max_chat_id, sent_id)

    # ── Startup tasks ─────────────────────────────────────────────────────

    async def fix_fallback_titles(self):
        """При старте переименовать все топики с fallback-названием 'Чат XXXXX'."""
        bindings = await self._repo.list_bindings()
        for binding in bindings:
            if not binding.title.startswith("Чат "):
                continue
            # Для DM-чатов chat_id == user_id собеседника — пробуем резолвить
            candidate_id = binding.max_chat_id
            name = await self._max.resolve_user_name(candidate_id)
            if name:
                await self._tg.rename_topic(binding.tg_topic_id, name)
                await self._repo.update_title(binding.max_chat_id, name)
                logger.info("Fixed fallback title: chat_id=%s %r → %r",
                            binding.max_chat_id, binding.title, name)
            else:
                logger.debug("Could not resolve name for chat_id=%s", binding.max_chat_id)

    # ── Cleanup ───────────────────────────────────────────────────────────

    async def run_cleanup(self):
        """Периодическая очистка старых записей. Запускать в фоне."""
        while True:
            await asyncio.sleep(1800)  # каждые 30 минут
            try:
                await self._repo.cleanup_old_messages(self._cfg.bridge.message_retention_days)
                await self._repo.cleanup_old_logs(self._cfg.bridge.log_retention_days)
                logger.info("Cleanup done")
            except Exception as e:
                logger.error("Cleanup error: %s", e)
