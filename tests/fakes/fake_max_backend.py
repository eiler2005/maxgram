"""In-memory MAX backend used to prove backend swappability without PyMax."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from src.adapters.max.ports import (
    MaxChatView,
    MaxClientMessage,
    MaxRawInterceptorResult,
    MaxSendResult,
    MaxUserView,
)


class FakeMaxClient:
    def __init__(self):
        self.logger = logging.getLogger("tests.fake_max_backend")
        self._connected = False
        self._closed = asyncio.Event()
        self._start_handlers = []
        self._message_handlers = []
        self._message_edit_handlers = []
        self._message_delete_handlers = []
        self._typing_handlers = []
        self._message_read_handlers = []
        self._presence_handlers = []
        self._reaction_update_handlers = []
        self._raw_receive_handlers = []
        self._raw_interceptor = None
        self.sent_messages: list[dict[str, Any]] = []
        self.users = {
            101: MaxUserView(id=101, display_name="Fake Sender"),
            9000: MaxUserView(id=9000, display_name="Bridge Account"),
        }
        self.group_chats = {
            -70000000000003: MaxChatView(id=-70000000000003, title="Fake MAX Chat")
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    def prepare_startup(self, error_handler):
        self._error_handler = error_handler

    def install_interactive_ping(self, ping_loop):
        self._ping_loop = ping_loop

    def install_raw_message_interceptor(self, handler):
        self._raw_interceptor = handler
        return MaxRawInterceptorResult(installed=False, reason="fake_backend")

    def register_start_handler(self, handler):
        self._start_handlers.append(handler)

    def register_raw_receive_handler(self, handler):
        self._raw_receive_handlers.append(handler)
        return len(self._raw_receive_handlers)

    def register_message_handler(self, handler):
        self._message_handlers.append(handler)

    def register_message_edit_handler(self, handler):
        self._message_edit_handlers.append(handler)

    def register_message_delete_handler(self, handler):
        self._message_delete_handlers.append(handler)

    def register_typing_handler(self, handler):
        self._typing_handlers.append(handler)

    def register_message_read_handler(self, handler):
        self._message_read_handlers.append(handler)

    def register_presence_handler(self, handler):
        self._presence_handlers.append(handler)

    def register_reaction_update_handler(self, handler):
        self._reaction_update_handlers.append(handler)

    async def get_message(self, *, chat_id: int, message_id: int):
        return None

    async def get_messages(self, *, chat_id: int, message_ids: list[int]):
        return []

    async def start(self):
        self._connected = True
        self._closed.clear()
        for handler in list(self._start_handlers):
            await handler()
        await self._closed.wait()

    async def close(self):
        self._connected = False
        self._closed.set()

    def own_user_id(self) -> str | None:
        return "9000"

    def cached_user(self, user_id: int) -> MaxUserView | None:
        return self.users.get(user_id)

    async def load_users(self, user_ids: list[int]) -> list[MaxUserView]:
        return [self.users[user_id] for user_id in user_ids if user_id in self.users]

    def contacts_snapshot(self) -> list[MaxUserView]:
        return list(self.users.values())

    def users_cache_snapshot(self) -> dict[object, MaxUserView]:
        return dict(self.users)

    def dialogs_snapshot(self) -> list:
        return []

    def group_chats_snapshot(self) -> list[MaxChatView]:
        return list(self.group_chats.values())

    def channels_snapshot(self) -> list:
        return []

    async def chat(self, chat_id: int) -> MaxChatView | None:
        return self.group_chats.get(chat_id)

    def dialog_last_message(self, chat_id: int) -> MaxClientMessage | None:
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
        message_id = f"fake-out-{len(self.sent_messages) + 1}"
        self.sent_messages.append(
            {
                "chat_id": str(chat_id),
                "text": text,
                "reply_to": str(reply_to) if reply_to is not None else None,
                "media_path": media_path,
                "media_type": media_type,
                "message_id": message_id,
            }
        )
        return MaxSendResult(message_id=message_id)

    async def raw_request(self, **kwargs) -> dict[str, Any] | None:
        return {}

    async def file_url(self, **kwargs) -> str | None:
        return None

    async def video_payload(self, **kwargs) -> dict[str, Any] | None:
        return None

    async def raw_history_payload(self, **kwargs) -> dict[str, Any] | None:
        return None

    async def history_messages(self, **kwargs) -> list[MaxClientMessage]:
        return []

    async def emit_text_message(
        self,
        *,
        chat_id: int = -70000000000003,
        msg_id: int = 1,
        sender_id: int = 101,
        text: str = "sample text",
    ):
        message = MaxClientMessage(
            id=msg_id,
            chat_id=chat_id,
            sender=sender_id,
            text=text,
            type="TEXT",
            attaches=[],
        )
        for handler in list(self._message_handlers):
            await handler(message)


class FakeMaxBackend:
    def __init__(self):
        self.client = FakeMaxClient()

    def create_client(self) -> FakeMaxClient:
        return self.client

    def make_file_attachment(self, path: str) -> Any:
        return SimpleNamespace(path=path, type="FILE")

    def make_photo_attachment(self, path: str) -> Any:
        return SimpleNamespace(path=path, type="PHOTO")

    def make_video_attachment(self, path: str) -> Any:
        return SimpleNamespace(path=path, type="VIDEO")

    def make_message_from_dict(self, payload: dict[str, Any]) -> Any:
        return MaxClientMessage.from_object(payload)

    def opcode(self, name: str, default: int | None = None) -> Any:
        return default

    def opcode_value(self, name: str, default: int) -> int:
        return default

    def opcode_name(self, value: object) -> str | None:
        return str(value) if value is not None else None

    def fetch_history_payload(self, *, chat_id: int, from_time: int, forward: int, backward: int) -> dict[str, Any]:
        return {
            "chatId": chat_id,
            "from": from_time,
            "forward": forward,
            "backward": backward,
        }

    def get_video_payload(self, *, chat_id: int, message_id: int, video_id: int) -> dict[str, Any]:
        return {"chatId": chat_id, "messageId": message_id, "videoId": video_id}
