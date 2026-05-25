"""Shared database DTOs and small serialization helpers."""

import json
from dataclasses import dataclass
from typing import Any, Optional


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str | None, default: Any):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


@dataclass
class ChatBinding:
    max_chat_id: str
    tg_topic_id: int
    title: str
    mode: str  # active | readonly | disabled
    created_at: int


@dataclass
class KnownUser:
    max_user_id: str
    display_name: str
    updated_at: int


@dataclass
class MessageRecord:
    max_msg_id: str
    max_chat_id: str
    tg_msg_id: Optional[int]
    tg_topic_id: Optional[int]
    direction: str  # inbound | outbound
    created_at: int


@dataclass
class TgReplyMapping:
    tg_msg_id: int
    max_chat_id: str
    max_msg_id: str
    tg_topic_id: Optional[int]
    source: str
    created_at: int


@dataclass
class ChatRecoveryEntry:
    registry_key: str
    tg_topic_id: Optional[int]
    title: str
    old_max_chat_id: Optional[str]
    current_max_chat_id: Optional[str]
    chat_kind: str
    mode: str
    priority: int
    access_type: Optional[str]
    invite_link: Optional[str]
    owner_user_id: Optional[str]
    owner_name: Optional[str]
    admin_contacts_json: str
    dm_partner_user_id: Optional[str]
    dm_partner_name: Optional[str]
    participant_count: Optional[int]
    manual_note: Optional[str]
    recovery_status: str
    first_seen_at: int
    last_seen_at: int
    last_scan_at: Optional[int]


@dataclass
class DmContactRecoveryEntry:
    max_user_id: str
    display_name: str
    old_dm_chat_id: Optional[str]
    current_dm_chat_id: Optional[str]
    tg_topic_id: Optional[int]
    source: str
    recovery_status: str
    first_seen_at: int
    last_seen_at: int
    last_scan_at: Optional[int]


@dataclass
class PendingMediaDownload:
    max_chat_id: str
    max_msg_id: str
    tg_topic_id: int
    attachment_index: int
    kind: str
    source_type: Optional[str]
    media_chat_id: str
    media_msg_id: str
    reference_kind: str
    reference_id: str
    filename: Optional[str] = None
    duration: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    status: str = "pending"
    attempts: int = 0
    created_at: int = 0
    updated_at: int = 0
    next_attempt_at: int = 0
    last_attempt_at: Optional[int] = None
    lease_until: Optional[int] = None
    last_error: Optional[str] = None
    delivered_tg_msg_id: Optional[int] = None
    delivered_at: Optional[int] = None
    id: Optional[int] = None


@dataclass
class PendingOutboundMessage:
    tg_topic_id: int
    tg_msg_id: int
    max_chat_id: str
    text: Optional[str]
    reply_to_max_id: Optional[str] = None
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: int = 0
    last_error: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0
    last_attempt_at: Optional[int] = None
    lease_until: Optional[int] = None
    delivered_max_msg_id: Optional[str] = None
    delivered_at: Optional[int] = None
    id: Optional[int] = None


@dataclass
class PendingInboundMessage:
    max_chat_id: str
    max_msg_id: str
    tg_topic_id: int
    text: Optional[str]
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: int = 0
    last_error: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0
    last_attempt_at: Optional[int] = None
    lease_until: Optional[int] = None
    delivered_tg_msg_id: Optional[int] = None
    delivered_at: Optional[int] = None
    id: Optional[int] = None
