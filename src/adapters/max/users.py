"""Pymax-free object/user extraction helpers."""

from collections.abc import Callable, Iterable
from typing import Optional


def enum_value(value) -> Optional[str]:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    raw = getattr(raw, "name", raw)
    text = str(raw).strip()
    return text or None


def extract_user_id(user_obj) -> Optional[str]:
    if user_obj is None:
        return None
    if isinstance(user_obj, (int, str)):
        text = str(user_obj).strip()
        return text or None
    for attr in ("id", "user_id", "account_id"):
        value = getattr(user_obj, attr, None)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def iter_userish(value, *, extract_user_name: Callable[[object], Optional[str]]) -> Iterable[object]:
    if value is None:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if extract_user_id(item) or extract_user_name(item):
                yield item
            else:
                yield key
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield item
        return
    yield value


def normalize_recovery_chat_kind(chat_obj) -> str:
    raw_type = enum_value(getattr(chat_obj, "type", None))
    if raw_type:
        lowered = raw_type.lower()
        if "channel" in lowered:
            return "channel"
        if "dialog" in lowered or "dm" in lowered or "private" in lowered:
            return "dm"
        if "chat" in lowered or "group" in lowered:
            return "group"

    try:
        chat_id_int = int(getattr(chat_obj, "id", 0))
        if chat_id_int < 0:
            return "group"
    except (TypeError, ValueError):
        pass
    return "unknown"


def chat_title(chat_obj, fallback: str) -> str:
    for attr in ("title", "name"):
        value = getattr(chat_obj, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def dialog_partner_id(dialog_obj, own_id: Optional[str], *, extract_user_name) -> Optional[str]:
    for participant in iter_userish(
        getattr(dialog_obj, "participants", None),
        extract_user_name=extract_user_name,
    ):
        candidate = extract_user_id(participant)
        if candidate and candidate != own_id:
            return candidate
    return None
