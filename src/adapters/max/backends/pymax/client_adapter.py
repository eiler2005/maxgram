from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ...ports import (
    MaxChatView,
    MaxClientMessage,
    MaxDialogView,
    MaxRawInterceptorResult,
    MaxSendResult,
    MaxUserView,
    RuntimeErrorHandler,
)
from .events import PymaxEventRouter
from .internals import (
    pymax_client_connection,
    pymax_connection_is_open,
    pymax_connection_lost,
    pymax_connection_transport_connected,
)
from .media import PymaxMediaGateway
from .models import (
    channels_snapshot,
    contacts_snapshot,
    dialogs_snapshot,
    group_chats_snapshot,
    own_user_id,
    users_cache,
)
from .raw_gateway import PymaxRawGateway


class PymaxClientAdapter:
    """Thin internal port over a PyMax 2 Client."""

    def __init__(self, client):
        self._client = client
        self._events = PymaxEventRouter(client)
        self._raw = PymaxRawGateway(client)
        self._media = PymaxMediaGateway(client, self._raw)

    @property
    def raw_client(self):
        return self._client

    @property
    def logger(self):
        return getattr(self._client, "logger", logging.getLogger("pymax"))

    @property
    def is_connected(self) -> bool:
        connection = pymax_client_connection(self._client)
        if connection is None:
            return False
        if pymax_connection_lost(connection):
            return False
        if not pymax_connection_is_open(connection):
            return False
        return pymax_connection_transport_connected(connection)

    def prepare_startup(self, error_handler: RuntimeErrorHandler) -> None:
        original = getattr(self._client, "start", None)
        if original is None or getattr(original, "_maxtg_wrapped", False):
            return

        async def wrapped_start(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            except Exception as exc:
                await error_handler(exc)
                raise

        wrapped_start._maxtg_wrapped = True  # type: ignore[attr-defined]
        setattr(self._client, "start", wrapped_start)

    def install_interactive_ping(self, _ping_loop: Callable[[], object]) -> None:
        """PyMax 2 has its own ping loop; keep port method as a no-op."""

    def install_raw_message_interceptor(self, handler) -> MaxRawInterceptorResult:
        return self._raw.install_raw_handler(handler)

    def register_start_handler(self, handler) -> None:
        self._events.register_start_handler(handler)

    def register_raw_receive_handler(self, handler) -> int | None:
        result = self._raw.install_raw_handler(handler)
        return result.raw_handler_count if result.installed else None

    def register_message_handler(self, handler) -> None:
        self._events.register_message_handler(handler)

    def register_message_edit_handler(self, handler) -> None:
        self._events.register_message_edit_handler(handler)

    def register_message_delete_handler(self, handler) -> None:
        self._events.register_message_delete_handler(handler)

    def register_typing_handler(self, handler) -> None:
        self._events.register_typing_handler(handler)

    def register_message_read_handler(self, handler) -> None:
        self._events.register_message_read_handler(handler)

    def register_presence_handler(self, handler) -> None:
        self._events.register_presence_handler(handler)

    def register_reaction_update_handler(self, handler) -> None:
        self._events.register_reaction_update_handler(handler)

    async def get_message(self, *, chat_id: int, message_id: int):
        result = await self._client.get_message(chat_id, message_id)
        if result is None:
            return None
        from ...ports import MaxClientMessage
        return MaxClientMessage.from_object(result)

    async def get_messages(self, *, chat_id: int, message_ids: list[int]):
        from ...ports import MaxClientMessage
        results = await self._client.get_messages(chat_id, message_ids)
        return [MaxClientMessage.from_object(m) for m in (results or [])]

    async def start(self):
        return await self._client.start()

    async def close(self):
        return await self._client.close()

    def own_user_id(self) -> str | None:
        return own_user_id(self._client)

    def cached_user(self, user_id: int) -> MaxUserView | None:
        user = self._client.get_cached_user(user_id)
        return MaxUserView.from_object(user)

    async def load_users(self, user_ids: list[int]) -> list[MaxUserView]:
        users = await self._client.get_users(user_ids)
        return [item for user in users or [] if (item := MaxUserView.from_object(user))]

    def contacts_snapshot(self) -> list[MaxUserView]:
        return contacts_snapshot(self._client)

    def users_cache_snapshot(self) -> dict[object, MaxUserView]:
        return users_cache(self._client)

    def dialogs_snapshot(self) -> list[MaxDialogView]:
        return dialogs_snapshot(self._client)

    def group_chats_snapshot(self) -> list[MaxChatView]:
        return group_chats_snapshot(self._client)

    def channels_snapshot(self) -> list[MaxChatView]:
        return channels_snapshot(self._client)

    async def chat(self, chat_id: int) -> MaxChatView | None:
        return MaxChatView.from_object(await self._client.get_chat(chat_id))

    def dialog_last_message(self, chat_id: int) -> MaxClientMessage | None:
        for dialog in self.dialogs_snapshot():
            if getattr(dialog, "id", None) == chat_id:
                return dialog.last_message
        return None

    async def send_outbound_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        media_path: str | None = None,
        media_type: str | None = None,
    ) -> MaxSendResult:
        return await self._media.send_outbound_message(
            chat_id=chat_id,
            text=text,
            reply_to=reply_to,
            media_path=media_path,
            media_type=media_type,
        )

    async def raw_request(
        self,
        *,
        opcode_name: str,
        payload: dict[str, Any],
        default_opcode: int | None = None,
        timeout: int | float | None = None,
        cmd: int | None = None,
    ) -> dict[str, Any] | None:
        return await self._raw.request(
            opcode_name=opcode_name,
            default_opcode=default_opcode,
            payload=payload,
            timeout=timeout,
            cmd=cmd,
        )

    async def file_url(self, *, chat_id: int, message_id: int, file_id: int) -> str | None:
        return await self._media.file_url(
            chat_id=chat_id,
            message_id=message_id,
            file_id=file_id,
        )

    async def video_payload(
        self, *, chat_id: int, message_id: int, video_id: int
    ) -> dict[str, Any] | None:
        return await self._media.video_payload(
            chat_id=chat_id,
            message_id=message_id,
            video_id=video_id,
        )

    async def raw_history_payload(
        self, *, chat_id: int, from_time: int, forward: int, backward: int
    ) -> dict[str, Any] | None:
        return await self._media.raw_history_payload(
            chat_id=chat_id,
            from_time=from_time,
            forward=forward,
            backward=backward,
        )

    async def history_messages(
        self, *, chat_id: int, from_time: int, forward: int, backward: int
    ):
        return await self._media.history_messages(
            chat_id=chat_id,
            from_time=from_time,
            forward=forward,
            backward=backward,
        )
