"""
Telegram Adapter — бот + форум-группа с Topics.

Ответственность:
  - Создание топиков (один MAX чат = один топик)
  - Отправка текста, фото, документов в нужный топик
  - Получение reply от пользователя → передача в Bridge Core
  - Команды: /status, /reauth
  - Уведомления владельцу (ошибки, потеря MAX сессии)
"""

import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional, Awaitable

from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)


ReplyHandler = Callable[[int, str, Optional[int], Optional[str]], Awaitable[None]]
# args: tg_topic_id, text, reply_to_tg_msg_id, sender_name


class TelegramAdapter:
    def __init__(self, bot_token: str, owner_id: int, forum_group_id: int):
        self._token = bot_token
        self._owner_id = owner_id
        self._group_id = forum_group_id
        self._bot: Optional[Bot] = None
        self._dp: Optional[Dispatcher] = None
        self._reply_handlers: list[ReplyHandler] = []

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

    # ── Отправка сообщений ────────────────────────────────────────────────

    async def send_text(self, topic_id: int, text: str,
                        reply_to_msg_id: Optional[int] = None) -> Optional[int]:
        """Отправить текст в топик. Возвращает message_id."""
        try:
            kwargs = dict(
                chat_id=self._group_id,
                text=text[:4096],
                message_thread_id=topic_id,
            )
            if reply_to_msg_id:
                kwargs["reply_to_message_id"] = reply_to_msg_id
            msg = await self._bot.send_message(**kwargs)
            return msg.message_id
        except TelegramAPIError as e:
            logger.error("send_text failed topic=%s: %s", topic_id, e)
            return None

    async def send_photo(self, topic_id: int, path: str, caption: str = "") -> Optional[int]:
        """Отправить фото в топик."""
        try:
            msg = await self._bot.send_photo(
                chat_id=self._group_id,
                photo=FSInputFile(path),
                caption=caption[:1024] if caption else None,
                message_thread_id=topic_id,
            )
            return msg.message_id
        except TelegramAPIError as e:
            logger.error("send_photo failed topic=%s: %s", topic_id, e)
            return None

    async def send_document(self, topic_id: int, path: str,
                             caption: str = "", filename: str = "") -> Optional[int]:
        """Отправить документ в топик."""
        try:
            msg = await self._bot.send_document(
                chat_id=self._group_id,
                document=FSInputFile(path, filename=filename or Path(path).name),
                caption=caption[:1024] if caption else None,
                message_thread_id=topic_id,
            )
            return msg.message_id
        except TelegramAPIError as e:
            logger.error("send_document failed topic=%s: %s", topic_id, e)
            return None

    async def send_video(self, topic_id: int, path: str, caption: str = "",
                         filename: str = "", duration: Optional[int] = None,
                         width: Optional[int] = None,
                         height: Optional[int] = None) -> Optional[int]:
        """Отправить видео в топик."""
        try:
            msg = await self._bot.send_video(
                chat_id=self._group_id,
                video=FSInputFile(path, filename=filename or Path(path).name),
                caption=caption[:1024] if caption else None,
                message_thread_id=topic_id,
                duration=duration,
                width=width,
                height=height,
                supports_streaming=True,
            )
            return msg.message_id
        except TelegramAPIError as e:
            logger.error("send_video failed topic=%s: %s", topic_id, e)
            return None

    async def send_audio(self, topic_id: int, path: str, caption: str = "",
                         filename: str = "", duration: Optional[int] = None) -> Optional[int]:
        """Отправить аудио в топик."""
        try:
            msg = await self._bot.send_audio(
                chat_id=self._group_id,
                audio=FSInputFile(path, filename=filename or Path(path).name),
                caption=caption[:1024] if caption else None,
                message_thread_id=topic_id,
                duration=duration,
                title=Path(filename or path).stem,
            )
            return msg.message_id
        except TelegramAPIError as e:
            logger.error("send_audio failed topic=%s: %s", topic_id, e)
            return None

    async def send_notification(self, text: str):
        """Отправить системное уведомление владельцу (в личный чат с ботом)."""
        try:
            await self._bot.send_message(chat_id=self._owner_id, text=text)
        except TelegramAPIError as e:
            logger.error("send_notification failed: %s", e)

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

    async def _dispatch_incoming_message(self, message: Message):
        # Игнорируем сообщения не из нашей группы
        if not self._is_group_message(message):
            return

        # Игнорируем сообщения от ботов, включая самого bridge-бота
        if message.from_user and message.from_user.is_bot:
            return

        # Команды доступны только владельцу
        if message.text and message.text.startswith("/"):
            if not self._is_owner(message):
                return
            await self._handle_command(message)
            return

        # Reply/сообщение в топике → bridge в MAX
        topic_id = message.message_thread_id
        if not topic_id:
            return

        reply_to_tg_id = None
        if message.reply_to_message:
            reply_to_tg_id = message.reply_to_message.message_id

        text = message.text or message.caption or ""
        if not text:
            return

        sender_name = self._render_sender_name(message)
        for handler in self._reply_handlers:
            try:
                await handler(topic_id, text, reply_to_tg_id, sender_name)
            except Exception as e:
                logger.error("reply handler error: %s", e)

    def _setup_handlers(self):
        @self._dp.message()
        async def handle_message(message: Message):
            await self._dispatch_incoming_message(message)

    async def _handle_command(self, message: Message):
        cmd = message.text.split()[0].lower()
        if cmd == "/status":
            await message.reply("✅ Bridge работает")
        elif cmd == "/reauth":
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
