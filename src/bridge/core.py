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
from ..logging_utils import build_max_flow_id, build_tg_flow_id, log_event, sanitize_path

logger = logging.getLogger(__name__)


class BridgeCore:
    def __init__(self, config: AppConfig, repo: Repository,
                 max_adapter: MaxAdapter, tg_adapter: TelegramAdapter):
        self._cfg = config
        self._repo = repo
        self._max = max_adapter
        self._tg = tg_adapter

        # Счётчики в памяти (накопительные с запуска)
        self._stats = {
            "start_time": time.time(),
            "inbound_text": 0,
            "inbound_media": 0,
            "outbound_text": 0,
            "outbound_media": 0,
            "failed_inbound": 0,
            "failed_outbound": 0,
        }

        # Регистрируем обработчики
        self._max.on_message(self._on_max_message)
        self._tg.on_reply(self._on_tg_reply)
        self._tg.on_command("status", self._build_status_message)
        self._tg.on_command("chats", self._build_chats_message)
        self._tg.on_command("help", self._build_help_message)
        self._tg.on_arg_command("dm", self._cmd_dm)

    # ── MAX → Telegram ────────────────────────────────────────────────────

    async def _on_max_message(self, msg: MaxMessage):
        """Входящее сообщение из MAX → форвардим в Telegram."""
        flow_id = build_max_flow_id(msg.chat_id, msg.msg_id)

        if not msg.msg_id or not msg.chat_id:
            return

        # Собственные сообщения: фильтруем эхо bridge-отправок, остальные форвардим
        if msg.is_own:
            if await self._repo.is_duplicate(msg.msg_id, msg.chat_id):
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

        # Дедупликация (для чужих сообщений)
        elif await self._repo.is_duplicate(msg.msg_id, msg.chat_id):
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

        # Персистим отправителя в known_users (для /dm поиска по имени)
        if msg.sender_id and msg.sender_name and not msg.is_own:
            await self._repo.save_user(msg.sender_id, msg.sender_name)

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
        topic_id = await self._get_or_create_topic(msg, flow_id=flow_id)
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
            await self._repo.log_delivery(msg.msg_id, msg.chat_id, "inbound", "failed",
                                          "no_topic")
            return

        # Проверяем режим чата
        binding = await self._repo.get_binding(msg.chat_id)
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

        # Форвардим в Telegram
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
        tg_msg_id = await self._forward_to_telegram(msg, topic_id, flow_id=flow_id)

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
            if msg.attachments:
                self._stats["inbound_media"] += 1
            else:
                self._stats["inbound_text"] += 1
            log_event(
                logger,
                logging.INFO,
                "bridge.inbound.forward_finished",
                flow_id=flow_id,
                direction="inbound",
                stage="forward",
                outcome="delivered",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=topic_id,
                tg_msg_id=tg_msg_id,
            )
        else:
            await self._repo.log_delivery(msg.msg_id, msg.chat_id, "inbound", "failed",
                                          "tg_send_failed")
            self._stats["failed_inbound"] += 1
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
            )

    async def _get_or_create_topic(self, msg: MaxMessage, *,
                                   flow_id: Optional[str] = None) -> Optional[int]:
        """Вернуть существующий topic_id или создать новый.
        Если топик уже есть, но имеет fallback-название — пробуем переименовать.
        """
        binding = await self._repo.get_binding(msg.chat_id)
        if binding:
            # Если название — fallback (ещё не знали имя), пробуем обновить
            if binding.title.startswith("Чат "):
                real_title = await self._resolve_chat_title(msg)
                if not real_title.startswith("Чат "):
                    await self._tg.rename_topic(binding.tg_topic_id, real_title, flow_id=flow_id)
                    await self._repo.update_title(msg.chat_id, real_title)
                    log_event(
                        logger,
                        logging.INFO,
                        "bridge.inbound.topic_resolved",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="routing",
                        outcome="renamed",
                        max_chat_id=msg.chat_id,
                        max_msg_id=msg.msg_id,
                        tg_topic_id=binding.tg_topic_id,
                        title=real_title,
                    )
                    return binding.tg_topic_id
            log_event(
                logger,
                logging.INFO,
                "bridge.inbound.topic_resolved",
                flow_id=flow_id,
                direction="inbound",
                stage="routing",
                outcome="existing",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=binding.tg_topic_id,
                title=binding.title,
            )
            return binding.tg_topic_id

        # Определяем название топика
        title = await self._resolve_chat_title(msg)

        # Создаём топик в Telegram
        try:
            topic_id = await self._tg.create_topic(title, flow_id=flow_id)
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "bridge.inbound.topic_resolved",
                flow_id=flow_id,
                direction="inbound",
                stage="routing",
                outcome="failed",
                reason="topic_create_failed",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                title=title,
                error=str(e),
            )
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
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound.topic_resolved",
            flow_id=flow_id,
            direction="inbound",
            stage="routing",
            outcome="created",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=topic_id,
            title=title,
            mode=mode,
        )
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

        # 3. Группы: live lookup названия через MAX API, если локальный cache miss
        if not msg.is_dm:
            title = await self._max.resolve_chat_title(msg.chat_id)
            if title:
                return title

        # 4. DM: резолвим имя СОБЕСЕДНИКА (не нашего аккаунта!) через MAX API.
        #
        #    Проблема: chat_id не всегда указывает на собеседника.
        #    Когда наш аккаунт инициирует чат (is_own=True), MAX может вернуть
        #    в echo chat_id == own_id, а sender_id тоже == own_id.
        #    Либо chat_id == Tatyana's ID, но resolve_user_name не возвращает имя
        #    (новый контакт), и код откатывается к sender_id == own_id.
        #
        #    Решение (три уровня):
        #      a) dialogs кеш pymax — надёжнее всего, явно видит обоих участников
        #      b) chat_id, если он != own_id (для входящих DM это всегда верно)
        #      c) sender_id, если != own_id и != chat_id (edge-case)
        #
        #    own_id НИКОГДА не попадает в кандидаты — он не может быть собеседником.
        if msg.is_dm:
            own_id = self._max.get_own_id()

            # a) Из кеша dialogs — самый надёжный источник
            dm_partner_id = self._max.get_dm_partner_id(msg.chat_id)

            # b) chat_id как кандидат — только если не наш ID
            chat_id_candidate = msg.chat_id if msg.chat_id != own_id else None

            # c) sender_id — только если отличается от уже имеющихся кандидатов и не наш
            sender_candidate = (
                msg.sender_id
                if (
                    msg.sender_id
                    and msg.sender_id != own_id
                    and msg.sender_id != dm_partner_id
                    and msg.sender_id != msg.chat_id
                )
                else None
            )

            candidates = list(dict.fromkeys(filter(None, [
                dm_partner_id,
                chat_id_candidate,
                sender_candidate,
            ])))
            for uid in candidates:
                name = await self._max.resolve_user_name(uid)
                if name:
                    return name

        # 5. Fallback
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
                               caption: str, *, flow_id: Optional[str] = None) -> Optional[int]:
        """Отправить одно вложение в Telegram."""
        if attachment.kind == "photo":
            return await self._tg.send_photo(topic_id, attachment.local_path, caption, flow_id=flow_id)

        if attachment.kind == "document":
            return await self._tg.send_document(
                topic_id, attachment.local_path, caption, attachment.filename or "", flow_id=flow_id
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
                flow_id=flow_id,
            )

        if attachment.kind == "audio":
            source_type = str(attachment.source_type or "").upper()
            if "VOICE" in source_type:
                return await self._tg.send_voice(
                    topic_id,
                    attachment.local_path,
                    caption,
                    duration=attachment.duration,
                    flow_id=flow_id,
                )
            return await self._tg.send_audio(
                topic_id,
                attachment.local_path,
                caption,
                attachment.filename or "",
                duration=attachment.duration,
                flow_id=flow_id,
            )

        placeholder = self._cfg.content.placeholder_unsupported.format(
            type=attachment.source_type or attachment.kind
        )
        return await self._tg.send_text(
            topic_id,
            self._compose_message_text(caption, placeholder),
            flow_id=flow_id,
        )

    async def _forward_to_telegram(self, msg: MaxMessage, topic_id: int,
                                   *, flow_id: Optional[str] = None) -> Optional[int]:
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
                sent_id = await self._tg.send_text(topic_id, text, flow_id=flow_id)
            else:
                caption = "" if emitted_anything else media_caption
                sent_id = await self._send_attachment(topic_id, attachment, caption, flow_id=flow_id)

            if sent_id:
                emitted_anything = True
                if tg_msg_id is None:
                    tg_msg_id = sent_id

        if extra_text:
            text = self._compose_message_text("" if emitted_anything else body_text, extra_text)
            sent_id = await self._tg.send_text(topic_id, text, flow_id=flow_id)
            if sent_id:
                emitted_anything = True
                if tg_msg_id is None:
                    tg_msg_id = sent_id

        if not emitted_anything and body_text:
            tg_msg_id = await self._tg.send_text(topic_id, body_text, flow_id=flow_id)

        elif not emitted_anything:
            cfg = self._cfg.content
            media_type = next((atype.lower() for atype in msg.attachment_types if atype), "unknown")
            placeholder = cfg.placeholder_unsupported.format(type=media_type)
            tg_msg_id = await self._tg.send_text(
                topic_id,
                self._compose_message_text(body_text, placeholder),
                flow_id=flow_id,
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

    async def _on_tg_reply(self, topic_id: int, tg_msg_id: Optional[int], text: str,
                           reply_to_tg_msg_id: Optional[int],
                           sender_name: Optional[str],
                           media_path: Optional[str] = None,
                           media_type: Optional[str] = None):
        """Reply из Telegram → отправляем в MAX."""
        flow_id = build_tg_flow_id(topic_id, tg_msg_id)
        log_event(
            logger,
            logging.INFO,
            "bridge.outbound.forward_started",
            flow_id=flow_id,
            direction="outbound",
            stage="received",
            outcome="accepted",
            tg_topic_id=topic_id,
            tg_msg_id=tg_msg_id,
            reply_to_tg_msg_id=reply_to_tg_msg_id,
            media_type=media_type,
            has_text=bool(text.strip()),
            filename=sanitize_path(media_path),
        )

        binding = await self._repo.get_binding_by_topic(topic_id)
        if not binding:
            await self._tg.send_notification(f"⚠️ Не найден MAX чат для топика {topic_id}")
            log_event(
                logger,
                logging.ERROR,
                "bridge.outbound.forward_finished",
                flow_id=flow_id,
                direction="outbound",
                stage="routing",
                outcome="failed",
                reason="no_topic",
                tg_topic_id=topic_id,
                tg_msg_id=tg_msg_id,
            )
            return

        if binding.mode == "readonly":
            await self._tg.send_text(
                topic_id,
                "🚫 Этот чат настроен как readonly — ответы не отправляются в MAX",
                flow_id=flow_id,
            )
            log_event(
                logger,
                logging.INFO,
                "bridge.outbound.forward_finished",
                flow_id=flow_id,
                direction="outbound",
                stage="routing",
                outcome="skipped",
                reason="readonly",
                tg_topic_id=topic_id,
                tg_msg_id=tg_msg_id,
                max_chat_id=binding.max_chat_id,
            )
            return

        if binding.mode == "disabled":
            log_event(
                logger,
                logging.INFO,
                "bridge.outbound.forward_finished",
                flow_id=flow_id,
                direction="outbound",
                stage="routing",
                outcome="skipped",
                reason="disabled",
                tg_topic_id=topic_id,
                tg_msg_id=tg_msg_id,
                max_chat_id=binding.max_chat_id,
            )
            return

        if media_path and self._is_file_too_large(media_path):
            max_size_mb = self._cfg.bridge.max_file_size_mb
            placeholder = self._cfg.content.placeholder_file_too_large.format(
                filename=Path(media_path).name
            )
            await self._tg.send_text(
                topic_id,
                f"🚫 {placeholder} (лимит: {max_size_mb}MB)",
                flow_id=flow_id,
            )
            try:
                Path(media_path).unlink(missing_ok=True)
            except Exception:
                pass
            self._stats["failed_outbound"] += 1
            log_event(
                logger,
                logging.ERROR,
                "bridge.outbound.forward_finished",
                flow_id=flow_id,
                direction="outbound",
                stage="validation",
                outcome="failed",
                reason="too_large",
                tg_topic_id=topic_id,
                tg_msg_id=tg_msg_id,
                max_chat_id=binding.max_chat_id,
                filename=sanitize_path(media_path),
            )
            return

        # Найти max_msg_id для reply (если есть)
        reply_to_max_id = None
        if reply_to_tg_msg_id:
            reply_to_max_id = await self._repo.get_max_msg_id_by_tg(reply_to_tg_msg_id)
        log_event(
            logger,
            logging.INFO,
            "bridge.outbound.reply_resolved",
            flow_id=flow_id,
            direction="outbound",
            stage="routing",
            outcome="found" if reply_to_max_id else "missing",
            tg_topic_id=topic_id,
            tg_msg_id=tg_msg_id,
            reply_to_tg_msg_id=reply_to_tg_msg_id,
            max_chat_id=binding.max_chat_id,
            reply_to_max_id=reply_to_max_id,
        )

        outbound_text = self._compose_tg_outbound_text(text, sender_name)
        sent_id = await self._max.send_message(
            chat_id=binding.max_chat_id,
            text=outbound_text,
            reply_to_msg_id=reply_to_max_id,
            media_path=media_path,
            media_type=media_type,
            flow_id=flow_id,
        )

        # Удаляем скачанный TG-файл после отправки
        if media_path:
            try:
                Path(media_path).unlink(missing_ok=True)
            except Exception:
                pass

        if sent_id is None:
            await self._tg.send_text(topic_id, "❌ Не удалось отправить сообщение в MAX", flow_id=flow_id)
            self._stats["failed_outbound"] += 1
            log_event(
                logger,
                logging.ERROR,
                "bridge.outbound.forward_finished",
                flow_id=flow_id,
                direction="outbound",
                stage="forward",
                outcome="failed",
                reason="max_send_failed",
                tg_topic_id=topic_id,
                tg_msg_id=tg_msg_id,
                max_chat_id=binding.max_chat_id,
            )
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
        if media_path:
            self._stats["outbound_media"] += 1
        else:
            self._stats["outbound_text"] += 1
        log_event(
            logger,
            logging.INFO,
            "bridge.outbound.forward_finished",
            flow_id=flow_id,
            direction="outbound",
            stage="forward",
            outcome="delivered",
            tg_topic_id=topic_id,
            tg_msg_id=tg_msg_id,
            max_chat_id=binding.max_chat_id,
            max_msg_id=sent_id,
            media_type=media_type,
        )

    # ── Status report ─────────────────────────────────────────────────────

    async def _build_status_message(self, period_hours: int = 4) -> str:
        """Сформировать текстовый статусный отчёт за period_hours часов."""
        since = int(time.time()) - period_hours * 3600

        msgs = await self._repo.count_messages_since(since)
        deliveries = await self._repo.count_deliveries_since(since)
        chat_activity = await self._repo.get_chat_activity_since(since, limit=10)
        all_bindings = await self._repo.list_bindings()

        # Uptime
        uptime_sec = int(time.time() - self._stats["start_time"])
        h, m = divmod(uptime_sec // 60, 60)
        uptime_str = f"{h}ч {m}м" if h else f"{m}м"

        # Соединения
        max_ok = "✅" if self._max.is_ready() else "❌"
        tg_ok = "✅"  # если мы дошли до /status — TG работает

        # Сообщения за период
        inbound_total = msgs.get("inbound", 0)
        outbound_total = msgs.get("outbound", 0)
        inbound_media = self._stats["inbound_media"]
        outbound_media = self._stats["outbound_media"]
        failed_in = self._stats["failed_inbound"]
        failed_out = self._stats["failed_outbound"]
        errors_total = failed_in + failed_out

        # Чаты
        total_chats = len(all_bindings)
        active_chats = sum(1 for b in all_bindings if b.mode == "active")

        lines = [
            f"📊 Bridge Status  ·  uptime: {uptime_str}",
            f"Период: последние {period_hours}ч",
            "",
            "🔗 Соединение",
            f"  MAX → Telegram  {max_ok}",
            f"  Telegram → MAX  {tg_ok}",
            "",
            f"📨 Сообщения (за {period_hours}ч)",
            f"  Входящих  (MAX→TG): {inbound_total}"
            + (f"  (медиа: {inbound_media})" if inbound_media else ""),
            f"  Исходящих (TG→MAX): {outbound_total}"
            + (f"  (медиа: {outbound_media})" if outbound_media else ""),
        ]
        if errors_total:
            lines.append(f"  ⚠️ Ошибок доставки: {errors_total}"
                         f"  (↓{failed_in} ↑{failed_out})")
        else:
            lines.append("  Ошибок: 0")

        if chat_activity:
            lines += ["", "💬 Активные чаты"]
            for c in chat_activity:
                title = (c["title"] or "—")[:30]
                lines.append(f"  {title:<32} ↓{c['inbound']}  ↑{c['outbound']}")

        lines += [
            "",
            f"🗂 Всего чатов: {total_chats}  (активных: {active_chats})",
        ]

        return "\n".join(lines)

    async def _build_chats_message(self, period_hours: int = 24) -> str:
        """Список чатов с topic_id, режимом и активностью за period_hours часов."""
        bindings = await self._repo.list_bindings()
        if not bindings:
            return "🗂 Чаты: 0"

        since = int(time.time()) - period_hours * 3600
        activity = await self._repo.get_chat_activity_map_since(since)

        mode_badge = {
            "active": "✅",
            "readonly": "🔒",
            "disabled": "⏸",
        }

        def sort_key(binding: ChatBinding) -> tuple[int, str]:
            stats = activity.get(binding.max_chat_id, {})
            return (int(stats.get("total", 0)), binding.title.lower())

        ordered = sorted(bindings, key=sort_key, reverse=True)
        total_chats = len(bindings)
        active_chats = sum(1 for b in bindings if b.mode == "active")

        lines = [
            f"🗂 Чаты: {total_chats} (активных: {active_chats})",
            f"Активность за {period_hours}ч:",
        ]

        max_rows = 40
        for index, binding in enumerate(ordered):
            if index >= max_rows:
                lines.append(f"... и ещё {total_chats - max_rows}")
                break
            stats = activity.get(binding.max_chat_id, {})
            inbound = int(stats.get("inbound", 0))
            outbound = int(stats.get("outbound", 0))
            badge = mode_badge.get(binding.mode, "•")
            title = (binding.title or f"Чат {binding.max_chat_id}").strip()
            lines.append(
                f"{badge} #{binding.tg_topic_id} {title[:42]} · ↓{inbound} ↑{outbound}"
            )

        return "\n".join(lines)

    async def _build_help_message(self) -> str:
        """Справка по командам bridge."""
        return (
            "ℹ️ MAX Bridge — пересылка чатов MAX ↔ Telegram\n"
            "\n"
            "Каждый MAX-чат = отдельный топик в этой группе.\n"
            "Reply в топике = ответ обратно в MAX.\n"
            "\n"
            "📋 Команды (только для владельца):\n"
            "  /status — состояние bridge, статистика за 4ч\n"
            "  /chats  — список чатов с активностью за 24ч\n"
            "  /help   — эта справка\n"
            "\n"
            "📩 Команда для всех участников группы (в топике General):\n"
            "  /dm Имя Фамилия текст — начать новый DM в MAX\n"
            "\n"
            "💡 Пример /dm (пишите в General):\n"
            "  /dm Татьяна Геннадиевна Ладина Добрый день!"
        )

    async def _cmd_dm(self, args: str) -> str:
        """Инициировать новый DM в MAX по имени пользователя.

        Формат: /dm Имя Фамилия текст сообщения
        Bridge ищет пользователя в contacts и dialogs кеше pymax.
        Топик в Telegram создаётся автоматически из echo-сообщения.
        """
        words = args.strip().split()
        if len(words) < 2:
            return (
                "⚠️ Формат: /dm Имя Фамилия текст сообщения\n"
                "Пример: /dm Татьяна Геннадиевна Ладина Добрый день!"
            )

        # Перебираем префиксы от длинного к короткому (до 4 слов имя, минимум 1 слово сообщение)
        found_user_id: Optional[str] = None
        found_name: Optional[str] = None
        message_text: Optional[str] = None

        for name_len in range(min(4, len(words) - 1), 0, -1):
            candidate_name = " ".join(words[:name_len])
            candidate_msg = " ".join(words[name_len:])
            if not candidate_msg.strip():
                continue
            # DB первым (персистентно, работает после рестарта)
            uid = await self._repo.find_user_by_name(candidate_name)
            # Fallback: in-memory кеш pymax
            if not uid:
                uid = self._max.find_user_by_name(candidate_name)
            if uid:
                found_user_id = uid
                found_name = candidate_name
                message_text = candidate_msg
                break

        if not found_user_id:
            preview = " ".join(words[:3])
            return (
                f"❌ Пользователь не найден: «{preview}…»\n"
                "Имя должно совпадать с отображаемым в MAX.\n"
                "Пользователь должен быть в контактах или ранее писать в известные чаты."
            )

        sent_id = await self._max.send_message(
            chat_id=found_user_id,
            text=message_text,
            flow_id="tg_cmd_dm",
        )
        if sent_id:
            return f"✅ Сообщение отправлено {found_name}. Топик появится автоматически."
        return f"❌ Не удалось отправить сообщение {found_name}."

    async def run_periodic_status(self, interval_hours: int = 4):
        """Автоматически отправлять статусный отчёт каждые interval_hours часов."""
        await asyncio.sleep(interval_hours * 3600)
        while True:
            try:
                text = await self._build_status_message(interval_hours)
                await self._tg.send_notification(text)
                logger.info("Periodic status sent")
            except Exception as e:
                logger.error("Periodic status error: %s", e)
            await asyncio.sleep(interval_hours * 3600)

    # ── Startup tasks ─────────────────────────────────────────────────────

    async def fix_fallback_titles(self):
        """При старте переименовать все топики с fallback-названием 'Чат XXXXX'."""
        bindings = await self._repo.list_bindings()
        for binding in bindings:
            if not binding.title.startswith("Чат "):
                continue
            # Для DM-чатов пробуем найти собеседника через dialogs кеш,
            # а не через chat_id напрямую — chat_id может совпадать с own_id
            # когда чат был инициирован нашим аккаунтом.
            own_id = self._max.get_own_id()
            candidate_id = (
                self._max.get_dm_partner_id(binding.max_chat_id)
                or (binding.max_chat_id if binding.max_chat_id != own_id else None)
            )
            if not candidate_id:
                continue
            name = await self._max.resolve_user_name(candidate_id)
            if name:
                await self._tg.rename_topic(binding.tg_topic_id, name)
                await self._repo.update_title(binding.max_chat_id, name)
                log_event(
                    logger,
                    logging.INFO,
                    "bridge.maintenance.fallback_title_fixed",
                    stage="maintenance",
                    outcome="renamed",
                    max_chat_id=binding.max_chat_id,
                    tg_topic_id=binding.tg_topic_id,
                    title=name,
                )
            else:
                log_event(
                    logger,
                    logging.DEBUG,
                    "bridge.maintenance.fallback_title_skipped",
                    stage="maintenance",
                    outcome="skipped",
                    reason="name_unresolved",
                    max_chat_id=binding.max_chat_id,
                    tg_topic_id=binding.tg_topic_id,
                )

    # ── MAX watchdog ──────────────────────────────────────────────────────

    async def run_max_watchdog(self,
                               alert_after_seconds: int = 60,
                               check_interval: int = 10):
        """Фоновая задача: следит за доступностью MAX.

        Если MAX недоступен дольше alert_after_seconds — отправляет уведомление
        владельцу. Повторное уведомление — только после восстановления и новой потери.
        """
        disconnected_since: Optional[float] = None
        alert_sent = False

        while True:
            await asyncio.sleep(check_interval)

            if self._max.is_ready():
                if alert_sent:
                    downtime = int(time.time() - disconnected_since)
                    await self._tg.send_notification(
                        f"⚠️ Возможен пропуск сообщений MAX за время простоя (~{downtime}с): "
                        "история во время disconnect не воспроизводится автоматически"
                    )
                    await self._tg.send_notification(
                        f"✅ MAX восстановлен (простой ~{downtime}с)"
                    )
                    log_event(
                        logger,
                        logging.INFO,
                        "bridge.watchdog.max_recovered",
                        stage="watchdog",
                        outcome="recovered",
                        downtime_seconds=downtime,
                    )
                disconnected_since = None
                alert_sent = False
            else:
                if disconnected_since is None:
                    disconnected_since = time.time()
                    log_event(
                        logger,
                        logging.WARNING,
                        "bridge.watchdog.max_lost",
                        stage="watchdog",
                        outcome="started",
                    )

                elapsed = time.time() - disconnected_since
                if not alert_sent and elapsed >= alert_after_seconds:
                    log_event(
                        logger,
                        logging.ERROR,
                        "bridge.watchdog.max_alert",
                        stage="watchdog",
                        outcome="alerted",
                        downtime_seconds=int(elapsed),
                    )
                    await self._tg.send_notification(
                        f"⚠️ MAX недоступен уже {int(elapsed)}с — идёт переподключение"
                    )
                    alert_sent = True

    # ── Cleanup ───────────────────────────────────────────────────────────

    async def run_cleanup(self):
        """Периодическая очистка старых записей. Запускать в фоне."""
        while True:
            await asyncio.sleep(1800)  # каждые 30 минут
            try:
                await self._repo.cleanup_old_messages(self._cfg.bridge.message_retention_days)
                await self._repo.cleanup_old_logs(self._cfg.bridge.log_retention_days)
                log_event(
                    logger,
                    logging.INFO,
                    "bridge.cleanup.completed",
                    stage="maintenance",
                    outcome="completed",
                    message_retention_days=self._cfg.bridge.message_retention_days,
                    log_retention_days=self._cfg.bridge.log_retention_days,
                )
            except Exception as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "bridge.cleanup.failed",
                    stage="maintenance",
                    outcome="failed",
                    error=str(e),
                )
