"""
Bridge contracts shared by core and adapters.

This module is intentionally transport-neutral: no pymax, aiogram, or adapter imports.
"""

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol


MAX_PROBABLE_CLIENT_CID_MIN = 1_000_000_000_000
MAX_DM_SWEEP_BACKFILL_SECONDS = 48 * 60 * 60


def is_probable_client_cid(value: object) -> bool:
    """MAX client-side cids are timestamp-like positive ids, not chat ids."""
    try:
        value_int = int(str(value))
    except (TypeError, ValueError):
        return False
    return value_int >= MAX_PROBABLE_CLIENT_CID_MIN


def is_usable_max_chat_id(value: object) -> bool:
    """MAX chat ids must be present, non-zero, and not client-side cids."""
    text = str(value or "").strip()
    if not text:
        return False
    try:
        value_int = int(text)
    except (TypeError, ValueError):
        return True
    return value_int != 0 and not is_probable_client_cid(value_int)


@dataclass
class MaxAttachment:
    """Нормализованное вложение из MAX."""

    kind: str                     # photo | video | audio | document
    local_path: str               # локальный путь к скачанному файлу
    filename: Optional[str]
    duration: Optional[int]
    width: Optional[int]
    height: Optional[int]
    source_type: Optional[str]    # исходный тип вложения в MAX/pymax


@dataclass
class MaxAttachmentFailure:
    """Метаданные вложения MAX, которое не удалось скачать."""

    kind: str
    source_type: Optional[str]
    filename: Optional[str]
    index: int
    reason: str
    retryable: bool = False
    media_chat_id: Optional[str] = None
    media_msg_id: Optional[str] = None
    reference_kind: Optional[str] = None
    reference_id: Optional[str] = None
    duration: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class MaxMessage:
    """Нормализованное сообщение из MAX."""

    msg_id: str
    chat_id: str
    chat_title: Optional[str]       # название группы или None для DM
    sender_id: Optional[str]
    sender_name: Optional[str]
    text: Optional[str]
    attachments: list[MaxAttachment]
    attachment_types: list[str]
    rendered_texts: list[str]
    message_type: Optional[str]
    status: Optional[str]
    is_dm: bool                     # True если это личная переписка
    is_own: bool                    # True если сообщение отправлено нашим аккаунтом
    raw: object                     # оригинальный объект библиотеки
    attachment_failures: list[MaxAttachmentFailure] = field(default_factory=list)


@dataclass
class MaxIssue:
    """Диагностическое состояние проблем с подключением к MAX."""

    kind: str
    summary: str
    raw_error: str
    requires_reauth: bool = False
    first_seen_at: int = field(default_factory=lambda: int(time.time()))
    last_seen_at: int = field(default_factory=lambda: int(time.time()))

    def signature(self) -> str:
        return f"{self.kind}:{self.summary}"


@dataclass
class MaxRecoveryChatSnapshot:
    max_chat_id: str
    title: str
    chat_kind: str
    access_type: Optional[str] = None
    invite_link: Optional[str] = None
    owner_user_id: Optional[str] = None
    owner_name: Optional[str] = None
    admin_contacts: list[dict[str, str]] = field(default_factory=list)
    dm_partner_user_id: Optional[str] = None
    dm_partner_name: Optional[str] = None
    participant_count: Optional[int] = None


@dataclass
class MaxRecoveryContactSnapshot:
    max_user_id: str
    display_name: str
    old_dm_chat_id: Optional[str] = None
    current_dm_chat_id: Optional[str] = None
    tg_topic_id: Optional[int] = None
    source: str = "dialog"
    recovery_status: str = "visible"


@dataclass
class MaxRecoverySnapshot:
    max_user_id: Optional[str]
    masked_phone: Optional[str]
    session_fingerprint_hash: Optional[str]
    chats: list[MaxRecoveryChatSnapshot] = field(default_factory=list)
    contacts: list[MaxRecoveryContactSnapshot] = field(default_factory=list)


@dataclass
class MaxTypingEvent:
    """A MAX user started typing in a chat."""
    chat_id: str
    user_id: str


@dataclass
class MaxReactionUpdate:
    """Standalone reaction-change event for an already-sent MAX message."""
    chat_id: str
    message_id: str
    total_count: int
    counters: list[dict]    # [{emoji: str, count: int}, ...]
    actor_user_id: Optional[str] = None
    actor_name: Optional[str] = None
    reaction: Optional[str] = None


MessageHandler = Callable[[MaxMessage], Awaitable[None]]
StartHandler = Callable[[], object]
IssueHandler = Callable[[MaxIssue], Optional[Awaitable[None]]]
TypingHandler = Callable[[MaxTypingEvent], Awaitable[None]]
ReactionUpdateHandler = Callable[[MaxReactionUpdate], Awaitable[None]]
ReplyHandler = Callable[
    [int, Optional[int], str, Optional[int], Optional[str], Optional[str], Optional[str]],
    Awaitable[None],
]
CommandHandler = Callable[..., Awaitable[str]]
ArgCommandHandler = Callable[[str], Awaitable[str]]


class MaxBridgePort(Protocol):
    """Transport-neutral operations BridgeCore needs from a MAX adapter."""

    def on_message(self, handler: MessageHandler) -> None: ...
    def on_start(self, handler: StartHandler) -> None: ...
    def on_issue(self, handler: IssueHandler) -> None: ...
    def on_typing(self, handler: TypingHandler) -> None: ...
    def on_reaction_update(self, handler: ReactionUpdateHandler) -> None: ...

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to_msg_id: Optional[str] = None,
        media_path: Optional[str] = None,
        media_type: Optional[str] = None,
        flow_id: Optional[str] = None,
    ) -> Optional[str]: ...

    async def resolve_user_name(self, user_id: str) -> Optional[str]: ...
    async def resolve_chat_title(self, chat_id: str) -> Optional[str]: ...
    def get_own_id(self) -> Optional[str]: ...
    def get_dm_partner_id(self, chat_id: str) -> Optional[str]: ...
    def find_user_by_name(self, name: str) -> Optional[str]: ...
    def is_ready(self) -> bool: ...
    def get_last_outbound_error(self) -> Optional[str]: ...
    def get_last_outbound_attempts(self) -> int: ...
    def get_last_issue(self) -> Optional[MaxIssue]: ...
    def get_last_connected_at(self) -> Optional[int]: ...
    def get_egress_status(self) -> dict[str, object] | None: ...
    def get_last_egress_probe(self) -> dict[str, object] | None: ...
    async def probe_egress(self) -> dict[str, object] | None: ...
    async def collect_recovery_snapshot(self) -> MaxRecoverySnapshot: ...
    async def download_video_reference(self, **kwargs) -> Optional[MaxAttachment]: ...
    async def download_audio_reference(self, **kwargs) -> Optional[MaxAttachment]: ...
    async def replay_recent_history(
        self,
        chat_id: str,
        *,
        limit: int = 30,
        since_ts=None,
        flow_id: Optional[str] = None,
        is_known_message: Optional[Callable[[str, str], Awaitable[bool]]] = None,
    ) -> int: ...
    def get_pending_empty_recovery_stats(self) -> dict[str, Optional[int]]: ...


class TelegramBridgePort(Protocol):
    """Transport-neutral operations BridgeCore needs from a Telegram adapter."""

    def on_reply(self, handler: ReplyHandler) -> None: ...
    def on_command(self, cmd: str, handler: CommandHandler) -> None: ...
    def on_arg_command(
        self,
        cmd: str,
        handler: ArgCommandHandler,
        *,
        allow_group_general: bool = False,
    ) -> None: ...

    async def create_topic(self, title: str, *, flow_id: Optional[str] = None) -> int: ...
    async def rename_topic(self, topic_id: int, new_title: str, *, flow_id: Optional[str] = None): ...
    async def delete_topic(self, topic_id: int, *, flow_id: Optional[str] = None) -> bool: ...
    async def close_topic(self, topic_id: int, *, flow_id: Optional[str] = None) -> bool: ...
    async def send_text(
        self,
        topic_id: int,
        text: str,
        reply_to_msg_id: Optional[int] = None,
        flow_id: Optional[str] = None,
    ) -> Optional[int]: ...
    async def send_photo(self, topic_id: int, path: str, caption: str = "", flow_id: Optional[str] = None) -> Optional[int]: ...
    async def send_document(
        self,
        topic_id: int,
        path: str,
        caption: str = "",
        filename: str = "",
        flow_id: Optional[str] = None,
    ) -> Optional[int]: ...
    async def send_video(
        self,
        topic_id: int,
        path: str,
        caption: str = "",
        filename: str = "",
        duration: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        flow_id: Optional[str] = None,
    ) -> Optional[int]: ...
    async def send_audio(
        self,
        topic_id: int,
        path: str,
        caption: str = "",
        filename: str = "",
        duration: Optional[int] = None,
        flow_id: Optional[str] = None,
    ) -> Optional[int]: ...
    async def send_voice(
        self,
        topic_id: int,
        path: str,
        caption: str = "",
        duration: Optional[int] = None,
        flow_id: Optional[str] = None,
    ) -> Optional[int]: ...
    async def send_owner_document(self, path: str, caption: str = "", filename: str = "") -> bool: ...
    async def send_system_notification(self, text: str, *, category: str = "system") -> bool: ...
    async def send_notification(self, text: str) -> bool: ...
    def get_last_send_error(self) -> Optional[str]: ...
    async def send_typing_indicator(self, topic_id: int) -> None: ...
    async def edit_message_text(self, msg_id: int, text: str) -> bool: ...


class OpsNotifierPort(Protocol):
    """Minimal notification surface for operational alerts."""

    async def send_system_notification(self, text: str, *, category: str = "system") -> bool: ...
    async def send_notification(self, text: str) -> bool: ...
