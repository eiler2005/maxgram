from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...ports import MaxChatView, MaxClientMessage, MaxDialogView, MaxUserView


@dataclass(frozen=True)
class MaxRawFrame:
    opcode: object | None
    cmd: object | None = None
    seq: object | None = None
    payload: dict[str, Any] | None = None

    @classmethod
    def from_object(cls, value: object) -> "MaxRawFrame":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            payload = value.get("payload")
            return cls(
                opcode=value.get("opcode"),
                cmd=value.get("cmd"),
                seq=value.get("seq"),
                payload=payload if isinstance(payload, dict) else None,
            )
        payload = getattr(value, "payload", None)
        return cls(
            opcode=getattr(value, "opcode", None),
            cmd=getattr(value, "cmd", None),
            seq=getattr(value, "seq", None),
            payload=payload if isinstance(payload, dict) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "opcode": self.opcode,
            "cmd": self.cmd,
            "seq": self.seq,
            "payload": self.payload or {},
        }


def normalize_frame(value: object) -> dict[str, Any]:
    return MaxRawFrame.from_object(value).to_dict()


def model_dump(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data = dump(by_alias=True, exclude_none=True)
        return data if isinstance(data, dict) else None
    raw = getattr(value, "__dict__", None)
    return dict(raw) if isinstance(raw, dict) else None


def chat_type_name(chat: object) -> str:
    value = getattr(chat, "type", None)
    value = getattr(value, "value", value)
    return str(value or "").upper()


def is_dialog_chat(chat: object) -> bool:
    return chat_type_name(chat) in {"DIALOG", "DM"}


def is_channel_chat(chat: object) -> bool:
    return chat_type_name(chat) == "CHANNEL"


def is_group_chat(chat: object) -> bool:
    name = chat_type_name(chat)
    return name in {"", "CHAT", "GROUP"}


def own_user_id(client: object) -> str | None:
    profile = getattr(client, "me", None)
    contact = getattr(profile, "contact", None)
    value = getattr(contact, "id", None)
    if value is None:
        value = getattr(profile, "id", None)
    return str(value) if value is not None else None


def users_cache(client: object) -> dict[object, MaxUserView]:
    users = getattr(client, "users", None)
    if users is None:
        app = getattr(client, "_app", None)
        users = getattr(app, "users", None)
    if users is None:
        users = getattr(client, "_users", None)
    if isinstance(users, dict):
        return {
            key: item
            for key, user in users.items()
            if (item := MaxUserView.from_object(user))
        }
    return {
        getattr(user, "id", index): item
        for index, user in enumerate(users or [])
        if (item := MaxUserView.from_object(user))
    }


def contacts_snapshot(client: object) -> list[MaxUserView]:
    return [
        item
        for user in (getattr(client, "contacts", None) or [])
        if (item := MaxUserView.from_object(user))
    ]


def dialogs_snapshot(client: object) -> list[MaxDialogView]:
    dialogs = getattr(client, "dialogs", None)
    if dialogs is not None:
        return [
            item
            for dialog in (dialogs or [])
            if (item := MaxDialogView.from_object(dialog))
        ]
    return [
        item
        for chat in (getattr(client, "chats", None) or [])
        if is_dialog_chat(chat) and (item := MaxDialogView.from_object(chat))
    ]


def group_chats_snapshot(client: object) -> list[MaxChatView]:
    return [
        item
        for chat in (getattr(client, "chats", None) or [])
        if is_group_chat(chat) and (item := MaxChatView.from_object(chat))
    ]


def channels_snapshot(client: object) -> list[MaxChatView]:
    channels = getattr(client, "channels", None)
    if channels is not None:
        return [
            item
            for channel in (channels or [])
            if (item := MaxChatView.from_object(channel))
        ]
    return [
        item
        for chat in (getattr(client, "chats", None) or [])
        if is_channel_chat(chat) and (item := MaxChatView.from_object(chat))
    ]


def client_message(value: object) -> MaxClientMessage:
    return MaxClientMessage.from_object(value)
