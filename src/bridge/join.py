"""Owner-confirmed MAX joins, independent of Telegram and PyMax models."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .contracts import (
    MaxBridgePort, MaxJoinedChat, MaxMessage, TelegramCallbackAction,
    TelegramInlineButton, TelegramResponse, is_usable_max_chat_id,
)
from .errors import MaxJoinError
from .max_links import extract_max_links, normalize_max_link
from ..db.repository import Repository
from ..db.types import _json_loads
from ..logging_utils import log_event

CONFIRM = "max_join_confirm"
CANCEL = "max_join_cancel"
ACTION_TYPE = "max_join_request"
CONFIRM_TTL_SECONDS = 15 * 60
JOIN_TIMEOUT_SECONDS = 30
JOIN_CACHE_SECONDS = 60
HELP = (
    "📎 Добавить канал или чат MAX\n"
    "Пришлите одну MAX-ссылку в личку боту или используйте /join ссылка "
    "в личке либо General. Поддерживаются max.ru/join/… и max.ru/имя_канала.\n"
    "Вступление произойдёт только после нажатия «Вступить в MAX» "
    "от имени подключённого MAX-аккаунта."
)
logger = logging.getLogger("src.bridge.core")


class MaxJoinService:
    def __init__(
        self, *, repo: Repository, max_adapter: MaxBridgePort,
        owner_id: int | None,
        ensure_topic: Callable[[MaxMessage], Awaitable[int | None]],
        topic_link: Callable[[int], str | None],
        schedule_scan: Callable[[str], None],
    ):
        self._repo = repo
        self._max = max_adapter
        self._owner_id = owner_id
        self._ensure_topic = ensure_topic
        self._topic_link = topic_link
        self._schedule = schedule_scan
        self._lock = asyncio.Lock()
        self._joined: dict[str, tuple[float, MaxJoinedChat]] = {}

    async def prepare(self, text: str) -> TelegramResponse:
        await self._repo.expire_join_requests(int(time.time()) - CONFIRM_TTL_SECONDS)
        links = extract_max_links(text)
        if len(links) != 1:
            if len(links) > 1:
                prefix = "Пришлите ссылки по одной.\n\n"
            else:
                prefix = "⚠️ Ссылка не подходит для вступления в MAX.\n\n" if text.strip() else ""
            return TelegramResponse(prefix + HELP)
        action_id = await self._repo.create_callback_action(
            action_type=ACTION_TYPE, max_chat_id="", max_msg_id="",
            source_type="telegram_join", payload={"url": links[0]},
        )
        return TelegramResponse(
            "Добавить этот канал или чат MAX?\n"
            f"Ссылка: {links[0]}\n"
            "Вступит MAX-аккаунт bridge. После подтверждения появится Telegram-топик.\n"
            "Кнопка действует 15 минут; старая история не загружается автоматически.",
            [TelegramInlineButton("Вступить в MAX", callback_data=f"{CONFIRM}:{action_id}"),
             TelegramInlineButton("Отмена", callback_data=f"{CANCEL}:{action_id}")],
        )

    async def _finish(self, action_id: str, error: str | None = None) -> None:
        await self._repo.mark_callback_action_used(
            action_id, error=error, clear_payload=True,
        )

    async def handle(self, callback: TelegramCallbackAction) -> str | TelegramResponse:
        if callback.user_id != self._owner_id:
            return "Только владелец bridge"
        record = await self._repo.get_callback_action(callback.action_id)
        if record is None or record.action_type != ACTION_TYPE:
            return "Действие не найдено"
        if record.status != "pending":
            return "Эта кнопка уже обработана или вступление выполняется"
        expired = int(time.time()) - record.created_at >= CONFIRM_TTL_SECONDS
        if expired or callback.action == CANCEL:
            if not await self._repo.claim_callback_action(record.id):
                return "Эта кнопка уже обработана"
            await self._finish(record.id, "expired" if expired else "cancelled")
            return TelegramResponse(
                "Кнопка устарела. Пришлите ссылку заново." if expired else "Вступление отменено."
            )
        if callback.action != CONFIRM:
            return "Неизвестное действие"
        if not self._max.is_ready():
            return "MAX сейчас не подключён. Повторите нажатие после восстановления связи."
        payload = _json_loads(record.payload_json, {})
        link = normalize_max_link(str(payload.get("url", ""))) if isinstance(payload, dict) else None
        if link is None:
            if await self._repo.claim_callback_action(record.id):
                await self._finish(record.id, "invalid_link")
            return TelegramResponse("MAX-ссылка недействительна. Пришлите её заново.")
        if not await self._repo.claim_callback_action(record.id):
            return "Вступление уже выполняется или кнопка обработана"
        async with self._lock:
            return await self._join(record.id, link)

    async def _join(self, action_id: str, link: str) -> TelegramResponse:
        try:
            now = time.monotonic()
            self._joined = {url: entry for url, entry in self._joined.items()
                            if now - entry[0] < JOIN_CACHE_SECONDS}
            cached = self._joined.get(link)
            joined = cached[1] if cached else None
            if joined is None:
                joined = await asyncio.wait_for(
                    self._max.join_chat_by_link(link), timeout=JOIN_TIMEOUT_SECONDS,
                )
            if joined.join_status == "pending":
                await self._finish(action_id)
                self._schedule("max_join_requested")
                return TelegramResponse(
                    "⏳ MAX принял заявку. Ожидаем одобрения администратора. "
                    "Это ещё не вступление; топик появится после допуска и нового сообщения."
                )
            if joined.join_status not in {"joined", "already_member"} or not is_usable_max_chat_id(joined.chat_id):
                raise MaxJoinError("unconfirmed")
            if str(joined.chat_kind or "").upper() in {"DIALOG", "DM", "USER"}:
                raise MaxJoinError("invalid_link")
            if len(self._joined) >= 256:
                self._joined.pop(next(iter(self._joined)))
            self._joined[link] = (time.monotonic(), joined)
            msg = MaxMessage(
                msg_id="", chat_id=str(joined.chat_id), chat_title=joined.title,
                sender_id=None, sender_name=None, text=None, attachments=[],
                attachment_types=[], rendered_texts=[], message_type=None,
                status=None, is_dm=False, is_own=False, raw=None,
            )
            topic_id = await self._ensure_topic(msg)
            if topic_id is None:
                await self._finish(action_id, "topic_failed")
                return TelegramResponse(
                    "В MAX вступили, но Telegram-топик создать не удалось. "
                    "Повторите /join с этой ссылкой, чтобы завершить подключение."
                )
            await self._finish(action_id)
            self._schedule("max_join_button")
            url = self._topic_link(topic_id)
            buttons = [TelegramInlineButton("Открыть топик в Telegram", url=url)] if url else []
            binding = await self._repo.get_binding(str(joined.chat_id))
            note = ("Пересылка для этого топика отключена."
                    if binding and binding.mode == "disabled"
                    else "Новые сообщения будут приходить туда.")
            return TelegramResponse(
                f"✅ Канал или чат подключён: {joined.title or 'MAX'}\n"
                f"Telegram-топик готов. {note}", buttons,
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, MaxJoinError) else "unconfirmed"
            await self._finish(action_id, code)
            log_event(logger, logging.WARNING, "bridge.join.failed", reason=code,
                      error_type=type(exc).__name__)
            messages = {
                "invalid_link": "Ссылка недействительна или приглашение истекло.",
                "denied": "MAX отказал в доступе. Возможно, нужны новое приглашение или права.",
                "already_member": "MAX сообщает, что вы уже участник. Найдите существующий топик через /chats.",
            }
            return TelegramResponse("⚠️ " + messages.get(
                code, "Не удалось подтвердить результат вступления. "
                "Проверьте /chats перед повторением /join.",
            ))
