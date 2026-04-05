"""
Telegram Adapter — бот + форум-группа с Topics.

Ответственность:
  - Создание топиков (один MAX чат = один топик)
  - Отправка текста, фото, документов в нужный топик
  - Получение reply от пользователя → передача в Bridge Core
  - Команды: /status, /chats, /reauth
  - Уведомления владельцу (ошибки, потеря MAX сессии)
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, Optional, Awaitable

from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

logger = logging.getLogger(__name__)


ReplyHandler = Callable[
    [int, str, Optional[int], Optional[str], Optional[str], Optional[str]],
    Awaitable[None],
]
# args: tg_topic_id, text, reply_to_tg_msg_id, sender_name, media_path, media_type


class TelegramAdapter:
    def __init__(self, bot_token: str, owner_id: int, forum_group_id: int,
                 tmp_dir: str = "/tmp"):
        self._token = bot_token
        self._owner_id = owner_id
        self._group_id = forum_group_id
        self._tmp_dir = Path(tmp_dir)
        self._bot: Optional[Bot] = None
        self._dp: Optional[Dispatcher] = None
        self._reply_handlers: list[ReplyHandler] = []
        self._command_handlers: dict[str, Callable] = {}

    def on_command(self, cmd: str, handler: Callable):
        """Зарегистрировать внешний обработчик команды (без слэша)."""
        self._command_handlers[cmd.lstrip("/")] = handler

    def on_reply(self, handler: ReplyHandler):
        self._reply_handlers.append(handler)

    # ── Топики ────────────────────────────────────────────────────────────

    async def create_topic(self, title: str) -> int:
        """Создать топик в форум-группе, вернуть message_thread_id."""
        result = await self._bot.create_forum_topic(
            chat_id=self._group_id,
            name=title[:128],  # Telegram limit
        )
        logger.info("Created topic %r thread_id=%s", title, result.message_thread_id)
        return result.message_thread_id

    async def rename_topic(self, topic_id: int, new_title: str):
        """Переименовать существующий топик."""
        try:
            await self._bot.edit_forum_topic(
                chat_id=self._group_id,
                message_thread_id=topic_id,
                name=new_title[:128],
            )
            logger.info("Renamed topic thread_id=%s → %r", topic_id, new_title)
        except TelegramAPIError as e:
            logger.error("rename_topic failed topic=%s: %s", topic_id, e)

    # ── Retry helper ──────────────────────────────────────────────────────

    async def _tg_retry(self, coro_fn, label: str) -> Optional[int]:
        """Выполнить TG API вызов с retry + exponential backoff.

        3 попытки: немедленно → sleep 1s → sleep 2s.
        TelegramRetryAfter: ждём retry_after секунд вместо стандартной задержки.
        Возвращает message_id при успехе, None после трёх неудач.
        """
        delays = (1, 2)  # пауза перед 2-й и 3-й попытками
        last_exc: Exception = RuntimeError("no attempt made")

        for attempt in range(1, 4):
            try:
                msg = await coro_fn()
                if attempt > 1:
                    logger.info("%s succeeded on attempt %d", label, attempt)
                return msg.message_id
            except TelegramRetryAfter as e:
                wait = max(int(e.retry_after), 1) + 1
                logger.warning("%s rate-limited (attempt %d/3), retry in %ds", label, attempt, wait)
                last_exc = e
                if attempt < 3:
                    await asyncio.sleep(wait)
            except TelegramAPIError as e:
                logger.warning("%s failed (attempt %d/3): %s", label, attempt, e)
                last_exc = e
                if attempt < 3:
                    await asyncio.sleep(delays[attempt - 1])

        logger.error("%s failed after 3 attempts: %s", label, last_exc)
        return None

    # ── Отправка сообщений ────────────────────────────────────────────────

    async def send_text(self, topic_id: int, text: str,
                        reply_to_msg_id: Optional[int] = None) -> Optional[int]:
        """Отправить текст в топик. Возвращает message_id."""
        kwargs: dict = dict(
            chat_id=self._group_id,
            text=text[:4096],
            message_thread_id=topic_id,
        )
        if reply_to_msg_id:
            kwargs["reply_to_message_id"] = reply_to_msg_id
        return await self._tg_retry(
            lambda: self._bot.send_message(**kwargs),
            f"send_text topic={topic_id}",
        )

    async def send_photo(self, topic_id: int, path: str, caption: str = "") -> Optional[int]:
        """Отправить фото в топик."""
        return await self._tg_retry(
            lambda: self._bot.send_photo(
                chat_id=self._group_id,
                photo=FSInputFile(path),
                caption=caption[:1024] if caption else None,
                message_thread_id=topic_id,
            ),
            f"send_photo topic={topic_id}",
        )

    async def send_document(self, topic_id: int, path: str,
                             caption: str = "", filename: str = "") -> Optional[int]:
        """Отправить документ в топик."""
        return await self._tg_retry(
            lambda: self._bot.send_document(
                chat_id=self._group_id,
                document=FSInputFile(path, filename=filename or Path(path).name),
                caption=caption[:1024] if caption else None,
                message_thread_id=topic_id,
            ),
            f"send_document topic={topic_id}",
        )

    async def send_video(self, topic_id: int, path: str, caption: str = "",
                         filename: str = "", duration: Optional[int] = None,
                         width: Optional[int] = None,
                         height: Optional[int] = None) -> Optional[int]:
        """Отправить видео в топик."""
        return await self._tg_retry(
            lambda: self._bot.send_video(
                chat_id=self._group_id,
                video=FSInputFile(path, filename=filename or Path(path).name),
                caption=caption[:1024] if caption else None,
                message_thread_id=topic_id,
                duration=duration,
                width=width,
                height=height,
                supports_streaming=True,
            ),
            f"send_video topic={topic_id}",
        )

    async def send_audio(self, topic_id: int, path: str, caption: str = "",
                         filename: str = "", duration: Optional[int] = None) -> Optional[int]:
        """Отправить аудио в топик."""
        return await self._tg_retry(
            lambda: self._bot.send_audio(
                chat_id=self._group_id,
                audio=FSInputFile(path, filename=filename or Path(path).name),
                caption=caption[:1024] if caption else None,
                message_thread_id=topic_id,
                duration=duration,
                title=Path(filename or path).stem,
            ),
            f"send_audio topic={topic_id}",
        )

    async def send_voice(self, topic_id: int, path: str,
                         caption: str = "", duration: Optional[int] = None) -> Optional[int]:
        """Отправить voice note в топик (нативный voice bubble)."""
        return await self._tg_retry(
            lambda: self._bot.send_voice(
                chat_id=self._group_id,
                voice=FSInputFile(path),
                caption=caption[:1024] if caption else None,
                message_thread_id=topic_id,
                duration=duration,
            ),
            f"send_voice topic={topic_id}",
        )

    async def send_notification(self, text: str):
        """Отправить системное уведомление владельцу (в личный чат с ботом)."""
        try:
            await self._bot.send_message(chat_id=self._owner_id, text=text)
        except TelegramAPIError as e:
            logger.error("send_notification failed: %s", e)

    # ── Скачивание медиа из Telegram ─────────────────────────────────────

    async def _download_tg_media(self, file_id: str, filename: str) -> Optional[str]:
        """Скачать медиафайл из Telegram в tmp_dir, вернуть локальный путь."""
        try:
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
            local_path = self._tmp_dir / filename
            await self._bot.download(file_id, destination=str(local_path))
            return str(local_path)
        except Exception as e:
            logger.error("TG media download failed file_id=%s: %s", file_id, e)
            return None

    # ── Получение reply ───────────────────────────────────────────────────

    def _is_owner(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id == self._owner_id)

    def _is_group_message(self, message: Message) -> bool:
        return bool(message.chat and message.chat.id == self._group_id)

    def _render_sender_name(self, message: Message) -> Optional[str]:
        user = getattr(message, "from_user", None)
        if not user:
            return None

        full_name = getattr(user, "full_name", None)
        if isinstance(full_name, str) and full_name.strip():
            return full_name.strip()

        parts = [
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        ]
        joined = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip()).strip()
        if joined:
            return joined

        username = getattr(user, "username", None)
        if isinstance(username, str) and username.strip():
            return f"@{username.strip()}"

        user_id = getattr(user, "id", None)
        return str(user_id) if user_id is not None else None

    def _is_owner_dm(self, message: Message) -> bool:
        """Личный чат владельца с ботом."""
        return bool(message.chat and message.chat.id == self._owner_id)

    async def _dispatch_incoming_message(self, message: Message):
        is_group = self._is_group_message(message)
        is_owner_dm = self._is_owner_dm(message)

        # Игнорируем всё, что не из нашей группы и не из личного чата владельца
        if not is_group and not is_owner_dm:
            return

        # Игнорируем сообщения от ботов, включая самого bridge-бота
        if message.from_user and message.from_user.is_bot:
            return

        # Команды: принимаем от владельца в группе или в личном чате
        if message.text and message.text.startswith("/"):
            if not self._is_owner(message):
                return
            await self._handle_command(message)
            return

        # Дальше — только сообщения из форум-группы (reply → MAX)
        if not is_group:
            return

        # Reply/сообщение в топике → bridge в MAX
        topic_id = message.message_thread_id
        if not topic_id:
            return

        reply_to_tg_id = None
        if message.reply_to_message:
            reply_to_tg_id = message.reply_to_message.message_id

        text = message.text or message.caption or ""

        # Скачиваем медиа если есть
        media_path: Optional[str] = None
        media_type: Optional[str] = None
        ts = int(time.time())

        if message.photo:
            media_path = await self._download_tg_media(
                message.photo[-1].file_id, f"tg_photo_{ts}.jpg"
            )
            media_type = "photo"
        elif message.video:
            ext = Path(message.video.file_name or "video.mp4").suffix or ".mp4"
            media_path = await self._download_tg_media(
                message.video.file_id, f"tg_video_{ts}{ext}"
            )
            media_type = "video"
        elif message.audio:
            ext = Path(message.audio.file_name or "audio.mp3").suffix or ".mp3"
            media_path = await self._download_tg_media(
                message.audio.file_id, f"tg_audio_{ts}{ext}"
            )
            media_type = "audio"
        elif message.voice:
            media_path = await self._download_tg_media(
                message.voice.file_id, f"tg_voice_{ts}.ogg"
            )
            media_type = "voice"
        elif message.document:
            fname = message.document.file_name or f"tg_doc_{ts}"
            media_path = await self._download_tg_media(message.document.file_id, fname)
            media_type = "document"

        if not text and not media_path:
            return

        sender_name = self._render_sender_name(message)
        for handler in self._reply_handlers:
            try:
                await handler(topic_id, text, reply_to_tg_id, sender_name, media_path, media_type)
            except Exception as e:
                logger.error("reply handler error: %s", e)

    def _setup_handlers(self):
        @self._dp.message()
        async def handle_message(message: Message):
            await self._dispatch_incoming_message(message)

    async def _handle_command(self, message: Message):
        cmd = message.text.split()[0].lstrip("/").lower()
        if cmd in self._command_handlers:
            try:
                reply_text = await self._command_handlers[cmd]()
                await message.reply(reply_text)
            except Exception as e:
                logger.error("Command handler /%s error: %s", cmd, e)
                await message.reply("⚠️ Ошибка при выполнении команды")
        elif cmd == "reauth":
            await message.reply(
                "⚠️ Для повторной авторизации MAX:\n"
                "Перезапусти bridge и введи новый SMS код."
            )

    # ── Жизненный цикл ────────────────────────────────────────────────────

    async def start(self):
        """Запустить polling (блокирующий)."""
        self._bot = Bot(token=self._token)
        self._dp = Dispatcher()
        self._setup_handlers()
        logger.info("Starting Telegram adapter owner=%s group=%s", self._owner_id, self._group_id)
        await self._dp.start_polling(self._bot, allowed_updates=["message"])

    async def setup(self) -> Bot:
        """Инициализировать бота без запуска polling (для использования в bridge)."""
        self._bot = Bot(token=self._token)
        self._dp = Dispatcher()
        self._setup_handlers()
        return self._bot

    def get_dispatcher(self) -> Dispatcher:
        return self._dp

    def get_bot(self) -> Bot:
        return self._bot
