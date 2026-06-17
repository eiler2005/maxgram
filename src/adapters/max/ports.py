from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, fields
from typing import Any, Optional, Protocol


RawReceiveHandler = Callable[[dict], Awaitable[object]]
ClientMessageHandler = Callable[["MaxClientMessage"], Awaitable[object]]
RuntimeErrorHandler = Callable[[BaseException], Awaitable[object]]


def _object_fields(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    data: dict[str, Any] = {}
    raw_fields = getattr(value, "__dict__", None)
    if isinstance(raw_fields, dict):
        data.update(raw_fields)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        for by_alias in (False, True):
            try:
                dumped = dump(by_alias=by_alias, exclude_none=True)
            except Exception:
                continue
            if isinstance(dumped, dict):
                data.update(dumped)
    extra = getattr(value, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        data.update(extra)
    if data:
        return data
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("__"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        result[name] = attr
    return result


def _field_names(cls: Any) -> set[str]:
    return {field.name for field in fields(cls)}


def _known_kwargs(cls: Any, data: dict[str, Any]) -> dict[str, Any]:
    known = _field_names(cls)
    return {key: data[key] for key in known if key in data}


def _copy_dynamic_fields(target: Any, data: dict[str, Any]) -> None:
    known = _field_names(target.__class__)
    for key, value in data.items():
        if key not in known:
            setattr(target, key, value)


def _display_name_from_user(value: object) -> Optional[str]:
    if value is None:
        return None
    display_name = getattr(value, "display_name", None)
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    names_list = getattr(value, "names", None)
    if names_list:
        first_name = ""
        last_name = ""
        first_item = names_list[0]
        first_name = (
            getattr(first_item, "first_name", None)
            or getattr(first_item, "name", None)
            or ""
        )
        last_name = getattr(first_item, "last_name", None) or ""
        name = f"{first_name} {last_name}".strip()
        if name:
            return name
    first = getattr(value, "first_name", None) or getattr(value, "name", None) or ""
    last = getattr(value, "last_name", None) or ""
    name = f"{first} {last}".strip()
    return name or None


@dataclass
class MaxClientAttachment:
    type: object | None = None
    id: object | None = None
    file_id: object | None = None
    fileId: object | None = None
    video_id: object | None = None
    videoId: object | None = None
    audio_id: object | None = None
    audioId: object | None = None
    url: str | None = None
    base_url: str | None = None
    baseRawUrl: str | None = None
    filename: str | None = None
    name: str | None = None
    duration: object | None = None
    width: object | None = None
    height: object | None = None
    event: str | None = None
    extra: object | None = None
    audio: object | None = None
    first_name: str | None = None
    last_name: str | None = None
    token: object | None = None

    @classmethod
    def from_object(cls, value: object) -> "MaxClientAttachment":
        if isinstance(value, cls):
            return value
        data = _object_fields(value)
        if "_type" in data and "type" not in data:
            data["type"] = data["_type"]
        item = cls(**_known_kwargs(cls, data))
        _copy_dynamic_fields(item, data)
        return item


@dataclass
class MaxClientLink:
    type: object | None = None
    chat_id: object | None = None
    message_id: object | None = None
    message: "MaxClientMessage | None" = None

    @classmethod
    def from_object(cls, value: object) -> "MaxClientLink | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        data = _object_fields(value)
        linked_message = data.get("message")
        if linked_message is not None:
            data["message"] = MaxClientMessage.from_object(linked_message)
        item = cls(**_known_kwargs(cls, data))
        _copy_dynamic_fields(item, data)
        return item


@dataclass
class MaxClientMessage:
    id: object | None = None
    chat_id: object | None = None
    sender: object | None = None
    text: str | None = None
    type: object | None = None
    status: object | None = None
    time: object | None = None
    attaches: list[MaxClientAttachment] | None = None
    link: MaxClientLink | None = None
    reactionInfo: object | None = None
    reaction_info: object | None = None
    _forward_source_chat_id: object | None = None
    _forward_source_msg_id: object | None = None
    _forward_link_type: object | None = None
    _from_raw_unwrapped: bool = False
    _from_empty_recovery: bool = False

    @classmethod
    def from_object(cls, value: object) -> "MaxClientMessage":
        if isinstance(value, cls):
            return value
        data = _object_fields(value)
        raw_attaches = data.get("attaches")
        if raw_attaches is None:
            raw_attaches = data.get("attachments")
        if raw_attaches is None:
            attaches = []
        elif isinstance(raw_attaches, list):
            attaches = [MaxClientAttachment.from_object(item) for item in raw_attaches]
        else:
            attaches = [MaxClientAttachment.from_object(raw_attaches)]
        data["attaches"] = attaches
        data["link"] = MaxClientLink.from_object(data.get("link"))
        for key in (
            "forwarded_message",
            "forward_message",
            "forwardedMessage",
            "forwardMessage",
            "channel_message",
            "channelMessage",
        ):
            if data.get(key) is not None:
                data[key] = MaxClientMessage.from_object(data[key])
        item = cls(**_known_kwargs(cls, data))
        _copy_dynamic_fields(item, data)
        return item


@dataclass
class MaxUserView:
    id: object | None = None
    display_name: str | None = None
    names: object | None = None
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    user_id: object | None = None
    account_id: object | None = None

    @classmethod
    def from_object(cls, value: object) -> "MaxUserView | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if isinstance(value, (int, str)):
            return cls(id=value)
        data = _object_fields(value)
        data.setdefault("display_name", _display_name_from_user(value))
        item = cls(**_known_kwargs(cls, data))
        _copy_dynamic_fields(item, data)
        return item


@dataclass
class MaxChatView:
    id: object | None = None
    title: str | None = None
    name: str | None = None
    type: object | None = None
    access: object | None = None
    access_type: object | None = None
    link: str | None = None
    invite_link: str | None = None
    owner: object | None = None
    admins: object | None = None
    admin_participants: object | None = None
    participants_count: object | None = None

    @classmethod
    def from_object(cls, value: object) -> "MaxChatView | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        data = _object_fields(value)
        item = cls(**_known_kwargs(cls, data))
        _copy_dynamic_fields(item, data)
        return item


@dataclass
class MaxDialogView:
    id: object | None = None
    participants: object | None = None
    title: str | None = None
    name: str | None = None
    type: object | None = "dm"
    last_message: MaxClientMessage | None = None

    @classmethod
    def from_object(cls, value: object) -> "MaxDialogView | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        data = _object_fields(value)
        if data.get("last_message") is not None:
            data["last_message"] = MaxClientMessage.from_object(data["last_message"])
        item = cls(**_known_kwargs(cls, data))
        _copy_dynamic_fields(item, data)
        return item


@dataclass
class MaxSendResult:
    message_id: str | None = None
    raw: object | None = None


@dataclass
class MaxRawInterceptorResult:
    installed: bool
    raw_handler_count: int = 0
    reason: str | None = None


class MaxClientPort(Protocol):
    logger: Any

    @property
    def is_connected(self) -> bool: ...
    def prepare_startup(self, error_handler: RuntimeErrorHandler) -> None: ...
    def install_interactive_ping(self, ping_loop: Callable[[], Awaitable[None]]) -> None: ...
    def install_raw_message_interceptor(
        self, handler: RawReceiveHandler
    ) -> MaxRawInterceptorResult: ...
    def register_start_handler(self, handler: Callable[[], Awaitable[object]]) -> None: ...
    def register_raw_receive_handler(self, handler: RawReceiveHandler) -> int | None: ...
    def register_message_handler(self, handler: ClientMessageHandler) -> None: ...
    def register_message_edit_handler(self, handler: ClientMessageHandler) -> None: ...
    def register_message_delete_handler(self, handler: ClientMessageHandler) -> None: ...
    def register_typing_handler(self, handler: ClientMessageHandler) -> None: ...
    def register_message_read_handler(self, handler: ClientMessageHandler) -> None: ...
    def register_presence_handler(self, handler: ClientMessageHandler) -> None: ...
    def register_reaction_update_handler(self, handler: ClientMessageHandler) -> None: ...
    async def get_message(self, *, chat_id: int, message_id: int) -> "MaxClientMessage | None": ...
    async def get_messages(self, *, chat_id: int, message_ids: list[int]) -> "list[MaxClientMessage]": ...
    async def start(self) -> object: ...
    async def close(self) -> object: ...
    def own_user_id(self) -> str | None: ...
    def cached_user(self, user_id: int) -> MaxUserView | None: ...
    async def load_users(self, user_ids: list[int]) -> list[MaxUserView]: ...
    def contacts_snapshot(self) -> list[MaxUserView]: ...
    def users_cache_snapshot(self) -> dict[object, MaxUserView]: ...
    def dialogs_snapshot(self) -> list[MaxDialogView]: ...
    def group_chats_snapshot(self) -> list[MaxChatView]: ...
    def channels_snapshot(self) -> list[MaxChatView]: ...
    async def chat(self, chat_id: int) -> MaxChatView | None: ...
    def dialog_last_message(self, chat_id: int) -> MaxClientMessage | None: ...
    async def send_outbound_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        media_path: str | None = None,
        media_type: str | None = None,
    ) -> MaxSendResult: ...
    async def raw_request(
        self,
        *,
        opcode_name: str,
        payload: dict[str, Any],
        default_opcode: int | None = None,
        timeout: int | float | None = None,
        cmd: int | None = None,
    ) -> dict[str, Any] | None: ...
    async def file_url(self, *, chat_id: int, message_id: int, file_id: int) -> str | None: ...
    async def video_payload(
        self, *, chat_id: int, message_id: int, video_id: int
    ) -> dict[str, Any] | None: ...
    async def raw_history_payload(
        self, *, chat_id: int, from_time: int, forward: int, backward: int
    ) -> dict[str, Any] | None: ...
    async def history_messages(
        self, *, chat_id: int, from_time: int, forward: int, backward: int
    ) -> Iterable[MaxClientMessage]: ...


async def maybe_await(value: object) -> object:
    if asyncio.iscoroutine(value):
        return await value
    return value
