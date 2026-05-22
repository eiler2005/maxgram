from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from types import SimpleNamespace
from typing import Any

from pymax.files import File, Photo, Video
from pymax.payloads import FetchHistoryPayload, GetVideoPayload
from pymax.static.enum import Opcode

from ...ports import (
    MaxChatView,
    MaxClientMessage,
    MaxDialogView,
    MaxRawInterceptorResult,
    MaxSendResult,
    MaxUserView,
    RuntimeErrorHandler,
)


class PymaxClientAdapter:
    """Typed internal port over the current pymax SocketMaxClient."""

    def __init__(self, client):
        self._client = client

    @property
    def raw_client(self):
        return self._client

    @property
    def logger(self):
        return self._client.logger

    @property
    def is_connected(self) -> bool:
        return bool(getattr(self._client, "is_connected", False))

    def prepare_startup(self, error_handler: RuntimeErrorHandler) -> None:
        for attr_name in ("_sync", "_login"):
            original = getattr(self._client, attr_name, None)
            if original is None or not asyncio.iscoroutinefunction(original):
                continue
            if getattr(original, "_maxtg_wrapped", False):
                continue

            async def wrapped(*args, __original=original, **kwargs):
                try:
                    return await __original(*args, **kwargs)
                except Exception as exc:
                    await error_handler(exc)
                    raise

            wrapped._maxtg_wrapped = True  # type: ignore[attr-defined]
            setattr(self._client, attr_name, wrapped)

    def install_interactive_ping(self, ping_loop: Callable[[], Awaitable[None]]) -> None:
        self._client._send_interactive_ping = ping_loop

    def install_raw_message_interceptor(self, handler) -> MaxRawInterceptorResult:
        if getattr(self._client, "_maxtg_raw_interceptor_installed", False):
            handler_count = len(getattr(self._client, "_on_raw_receive_handlers", []) or [])
            return MaxRawInterceptorResult(installed=True, raw_handler_count=handler_count)

        original = getattr(self._client, "_handle_message_notifications", None)
        if original is None:
            return MaxRawInterceptorResult(
                installed=False,
                reason="client_has_no_message_notification_handler",
            )

        async def _handle_message_notifications_with_raw(data: dict):
            await handler(data)
            return await original(data)

        _handle_message_notifications_with_raw._maxtg_wrapped = True  # type: ignore[attr-defined]
        self._client._handle_message_notifications = _handle_message_notifications_with_raw
        self._client._maxtg_raw_interceptor_installed = True
        handler_count = len(getattr(self._client, "_on_raw_receive_handlers", []) or [])
        return MaxRawInterceptorResult(installed=True, raw_handler_count=handler_count)

    def register_start_handler(self, handler) -> None:
        self._client.on_start(handler)

    def register_raw_receive_handler(self, handler) -> int | None:
        register = getattr(self._client, "on_raw_receive", None)
        if register is None:
            return None
        register(handler)
        return len(getattr(self._client, "_on_raw_receive_handlers", []) or [])

    def _wrap_message_handler(self, handler):
        async def wrapped(message):
            return await handler(MaxClientMessage.from_object(message))

        return wrapped

    def register_message_handler(self, handler) -> None:
        self._client.on_message()(self._wrap_message_handler(handler))

    def register_message_edit_handler(self, handler) -> None:
        self._client.on_message_edit()(self._wrap_message_handler(handler))

    def register_message_delete_handler(self, handler) -> None:
        self._client.on_message_delete()(self._wrap_message_handler(handler))

    async def start(self):
        return await self._client.start()

    async def close(self):
        return await self._client.close()

    def own_user_id(self) -> str | None:
        me = getattr(self._client, "me", None)
        if me is None:
            return None
        value = getattr(me, "id", None)
        return str(value) if value is not None else None

    def cached_user(self, user_id: int) -> MaxUserView | None:
        user = self._client.get_cached_user(user_id)
        return MaxUserView.from_object(user)

    async def load_users(self, user_ids: list[int]) -> list[MaxUserView]:
        users = await self._client.get_users(user_ids)
        return [item for user in users or [] if (item := MaxUserView.from_object(user))]

    def contacts_snapshot(self) -> list[MaxUserView]:
        return [
            item
            for user in (getattr(self._client, "contacts", None) or [])
            if (item := MaxUserView.from_object(user))
        ]

    def users_cache_snapshot(self) -> dict[object, MaxUserView]:
        users_cache = getattr(self._client, "_users", None) or {}
        return {
            key: item
            for key, user in users_cache.items()
            if (item := MaxUserView.from_object(user))
        }

    def dialogs_snapshot(self) -> list[MaxDialogView]:
        return [
            item
            for dialog in (getattr(self._client, "dialogs", None) or [])
            if (item := MaxDialogView.from_object(dialog))
        ]

    def group_chats_snapshot(self) -> list[MaxChatView]:
        return [
            item
            for chat in (getattr(self._client, "chats", None) or [])
            if (item := MaxChatView.from_object(chat))
        ]

    def channels_snapshot(self) -> list[MaxChatView]:
        return [
            item
            for channel in (getattr(self._client, "channels", None) or [])
            if (item := MaxChatView.from_object(channel))
        ]

    async def chat(self, chat_id: int) -> MaxChatView | None:
        return MaxChatView.from_object(await self._client.get_chat(chat_id))

    def dialog_last_message(self, chat_id: int) -> MaxClientMessage | None:
        for dialog in self.dialogs_snapshot():
            if getattr(dialog, "id", None) == chat_id:
                return dialog.last_message
        return None

    def _make_attachment(self, *, media_path: str | None, media_type: str | None):
        if not media_path:
            return None
        if media_type == "photo":
            return Photo(path=media_path)
        if media_type == "video":
            return Video(path=media_path)
        return File(path=media_path)

    async def send_outbound_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        media_path: str | None = None,
        media_type: str | None = None,
    ) -> MaxSendResult:
        kwargs: dict[str, object] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            kwargs["reply_to"] = reply_to
        attachment = self._make_attachment(media_path=media_path, media_type=media_type)
        if attachment is not None:
            kwargs["attachment"] = attachment
        result = await self._client.send_message(**kwargs)
        return MaxSendResult(message_id=self._extract_result_msg_id(result), raw=result)

    def _opcode(self, name: str, default: int | None = None):
        value = getattr(Opcode, name, None)
        if value is not None:
            return value
        if default is None:
            return None
        return SimpleNamespace(value=default, name=name)

    async def raw_request(
        self,
        *,
        opcode_name: str,
        payload: dict[str, Any],
        default_opcode: int | None = None,
        timeout: int | float | None = None,
        cmd: int | None = None,
    ) -> dict[str, Any] | None:
        opcode = self._opcode(opcode_name, default_opcode)
        if opcode is None:
            return None
        kwargs: dict[str, object] = {"opcode": opcode, "payload": payload}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if cmd is not None:
            kwargs["cmd"] = cmd
        return await self._client._send_and_wait(**kwargs)

    async def file_url(self, *, chat_id: int, message_id: int, file_id: int) -> str | None:
        file_obj = await self._client.get_file_by_id(
            chat_id=chat_id,
            message_id=message_id,
            file_id=file_id,
        )
        url = getattr(file_obj, "url", None)
        return str(url) if url else None

    async def video_payload(
        self, *, chat_id: int, message_id: int, video_id: int
    ) -> dict[str, Any] | None:
        payload = GetVideoPayload(
            chat_id=chat_id,
            message_id=message_id,
            video_id=video_id,
        ).model_dump(by_alias=True)
        data = await self.raw_request(opcode_name="VIDEO_PLAY", payload=payload)
        raw_payload = data.get("payload") if isinstance(data, dict) else None
        return raw_payload if isinstance(raw_payload, dict) else None

    async def raw_history_payload(
        self, *, chat_id: int, from_time: int, forward: int, backward: int
    ) -> dict[str, Any] | None:
        payload = FetchHistoryPayload(
            chat_id=chat_id,
            from_time=from_time,
            forward=forward,
            backward=backward,
        ).model_dump(by_alias=True)
        data = await self.raw_request(
            opcode_name="CHAT_HISTORY",
            default_opcode=49,
            payload=payload,
            timeout=10,
        )
        raw_payload = data.get("payload") if isinstance(data, dict) else None
        return raw_payload if isinstance(raw_payload, dict) else None

    async def history_messages(
        self, *, chat_id: int, from_time: int, forward: int, backward: int
    ) -> Iterable[MaxClientMessage]:
        fetch = getattr(self._client, "fetch_history", None)
        if fetch is None:
            return []
        messages = await fetch(
            chat_id,
            from_time=from_time,
            forward=forward,
            backward=backward,
        )
        return [MaxClientMessage.from_object(message) for message in messages or []]

    def _extract_result_msg_id(self, result) -> str | None:
        if result is None:
            return None

        direct_id = getattr(result, "id", None) or getattr(result, "message_id", None)
        if direct_id is not None:
            return str(direct_id)

        def from_dict(data) -> str | None:
            if not isinstance(data, dict):
                return None
            for key in ("id", "messageId", "message_id"):
                if data.get(key) is not None:
                    return str(data[key])
            for key in ("message", "payload", "result", "msg"):
                found = from_dict(data.get(key))
                if found:
                    return found
            return None

        return from_dict(result)
