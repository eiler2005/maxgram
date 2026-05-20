"""
MAX Adapter — подключение к MAX через SocketMaxClient (pymax).

Ответственность:
  - Авторизация (сессия уже сохранена в data/)
  - Получение входящих сообщений
  - Скачивание медиафайлов
  - Отправка сообщений
  - Reconnect при обрыве
  - Резолвинг имён пользователей для DM чатов
"""

import asyncio
import json
import logging
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional, Awaitable
from urllib.parse import urlparse
from urllib.parse import parse_qs

from aiohttp import ClientResponseError, ClientSession

from ..logging_utils import (
    build_max_flow_id,
    log_event,
    mask_phone,
    sanitize_path,
    sanitize_url,
)

logger = logging.getLogger(__name__)

MAX_CDN_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)
MAX_CDN_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
MAX_CDN_IOS_CHROME_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/136.0.0.0 "
    "Mobile/15E148 Safari/604.1"
)
MAX_CDN_ANDROID_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Mobile) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Mobile Safari/537.36"
)
MAX_DOWNLOAD_ATTEMPTS = 7
MAX_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
MAX_RAW_HISTORY_CACHE_TTL_SECONDS = 180
MAX_RAW_HISTORY_CACHE_SIZE = 256
MAX_RAW_HISTORY_EXPECTED_TTL_SECONDS = 30
MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS = 180
MAX_EMPTY_RECOVERY_CACHE_POLL_SECONDS = 1.0
MAX_EMPTY_RECOVERY_RETRY_POLL_SECONDS = 30
MAX_EMPTY_RECOVERY_RETRY_BASE_SECONDS = 60
MAX_EMPTY_RECOVERY_RETRY_MAX_SECONDS = 6 * 60 * 60
MAX_EMPTY_RECOVERY_STATE_FILE = "pending_empty_recoveries.json"
MAX_PROBABLE_CLIENT_CID_MIN = 1_000_000_000_000
MAX_DM_SWEEP_BACKFILL_SECONDS = 48 * 60 * 60
MAX_HISTORY_SWEEP_DIAGNOSTIC_TTL_SECONDS = 10 * 60


def is_probable_client_cid(value: object) -> bool:
    """MAX client-side cids are timestamp-like positive ids, not chat ids."""
    try:
        value_int = int(str(value))
    except (TypeError, ValueError):
        return False
    return value_int >= MAX_PROBABLE_CLIENT_CID_MIN


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
class PendingOutboundAck:
    """Ожидаем подтверждение исходящего сообщения по эху из MAX."""
    chat_id: str
    text: str
    reply_to_msg_id: Optional[str]
    created_monotonic: float
    future: asyncio.Future


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
class ForwardedPayload:
    """Развёрнутое содержимое forward/channel сообщения MAX."""
    message: object
    chat_id: Optional[str]
    msg_id: Optional[str]
    link_type: Optional[str]


@dataclass
class OutboundFailureState:
    """Последняя ошибка исходящей отправки в MAX."""
    error: Optional[str]
    attempts: int = 0


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


MessageHandler = Callable[[MaxMessage], Awaitable[None]]


class MaxAdapter:
    def __init__(self, phone: str, data_dir: str, session_name: str, tmp_dir: str):
        self._phone = phone
        self._data_dir = data_dir
        self._session_name = Path(session_name).name
        self._tmp_dir = Path(tmp_dir)
        self._client = None
        self._handlers: list[MessageHandler] = []
        self._started = False
        self._start_handlers: list[Callable] = []
        self._issue_handlers: list[Callable[[MaxIssue], Optional[Awaitable[None]]]] = []
        self._own_id: Optional[str] = None  # ID нашего аккаунта в MAX
        self._pending_outbound_acks: list[PendingOutboundAck] = []
        self._expected_outbound_ids: dict[tuple[str, str], float] = {}
        self._raw_unwrapped_message_ids: dict[tuple[str, str], float] = {}
        self._interactive_ping_failure_limit = 3
        self._last_outbound_failure = OutboundFailureState(error=None, attempts=0)
        self._last_start_error: Optional[str] = None
        self._last_issue: Optional[MaxIssue] = None
        self._last_issue_notification_signature: Optional[str] = None
        self._last_connected_at: Optional[int] = None
        self._raw_processed_message_ids: dict[tuple[str, str], float] = {}
        self._raw_history_messages: dict[tuple[str, str], tuple[float, object]] = {}
        self._expected_raw_history_messages: dict[str, tuple[str, float]] = {}
        self._pending_empty_recovery_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._pending_empty_recoveries: dict[str, dict[str, object]] = {}
        self._pending_empty_recovery_worker: Optional[asyncio.Task] = None
        self._history_sweep_diagnostic_log_until: dict[tuple[str, str, str], float] = {}
        self._load_pending_empty_recoveries()

    def on_message(self, handler: MessageHandler):
        self._handlers.append(handler)

    def on_start(self, handler: Callable):
        self._start_handlers.append(handler)

    def on_issue(self, handler: Callable[[MaxIssue], Optional[Awaitable[None]]]):
        self._issue_handlers.append(handler)

    def _normalize_outbound_text(self, text: Optional[str]) -> str:
        return (text or "").strip()

    def _set_last_outbound_failure(self, error: Optional[str], *, attempts: int = 0):
        self._last_outbound_failure = OutboundFailureState(error=error, attempts=attempts)

    def get_last_outbound_error(self) -> Optional[str]:
        return self._last_outbound_failure.error

    def get_last_outbound_attempts(self) -> int:
        return self._last_outbound_failure.attempts

    def get_last_start_error(self) -> Optional[str]:
        return self._last_start_error

    def get_last_issue(self) -> Optional[MaxIssue]:
        return self._last_issue

    def get_last_connected_at(self) -> Optional[int]:
        return self._last_connected_at

    def _clear_runtime_issue(self):
        self._last_start_error = None
        self._last_issue = None
        self._last_issue_notification_signature = None

    def _classify_runtime_error(self, error: BaseException) -> Optional[MaxIssue]:
        raw_error = str(error).strip() or error.__class__.__name__
        lowered = raw_error.lower()

        corrupt_session_markers = (
            "unsupported file format",
            "database disk image is malformed",
        )
        if any(marker in lowered for marker in corrupt_session_markers):
            return MaxIssue(
                kind="session_corrupt",
                summary="MAX session.db повреждён или не читается",
                raw_error=raw_error,
                requires_reauth=True,
            )

        invalid_token_markers = (
            "invalid token",
            "login.token",
            "авторизируйтесь снова",
            "please, login again",
        )
        if any(marker in lowered for marker in invalid_token_markers):
            return MaxIssue(
                kind="session_invalid",
                summary="MAX сессия недействительна, нужна повторная авторизация",
                raw_error=raw_error,
                requires_reauth=True,
            )

        if "must be online session" in lowered or "недопустимое состояние сессии" in lowered:
            return MaxIssue(
                kind="session_offline",
                summary="MAX сессия не перешла в ONLINE-состояние",
                raw_error=raw_error,
                requires_reauth=False,
            )

        return None

    def _remember_runtime_issue(self, issue: MaxIssue) -> MaxIssue:
        now = int(time.time())
        if (
            self._last_issue is not None
            and self._last_issue.kind == issue.kind
            and self._last_issue.summary == issue.summary
        ):
            self._last_issue.raw_error = issue.raw_error
            self._last_issue.last_seen_at = now
            self._last_issue.requires_reauth = issue.requires_reauth
            return self._last_issue

        issue.first_seen_at = now
        issue.last_seen_at = now
        self._last_issue = issue
        return issue

    async def _emit_runtime_issue(self, issue: MaxIssue):
        signature = issue.signature()
        if self._last_issue_notification_signature == signature:
            return

        self._last_issue_notification_signature = signature
        for handler in self._issue_handlers:
            try:
                result = handler(issue)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "max.adapter.issue_handler_failed",
                    stage="runtime",
                    outcome="failed",
                    issue_kind=issue.kind,
                    error=str(e),
                )

    async def _capture_runtime_error(self, error: BaseException):
        self._last_start_error = str(error).strip() or error.__class__.__name__
        issue = self._classify_runtime_error(error)
        if issue is not None:
            issue = self._remember_runtime_issue(issue)
            await self._emit_runtime_issue(issue)

    def _wrap_client_stage(self, client, attr_name: str):
        original = getattr(client, attr_name, None)
        if original is None or not asyncio.iscoroutinefunction(original):
            return
        if getattr(original, "_maxtg_wrapped", False):
            return

        async def wrapped(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            except Exception as e:
                await self._capture_runtime_error(e)
                raise

        wrapped._maxtg_wrapped = True  # type: ignore[attr-defined]
        setattr(client, attr_name, wrapped)

    def _is_retryable_send_error(self, error: BaseException) -> bool:
        if isinstance(
            error,
            (
                asyncio.TimeoutError,
                TimeoutError,
                ConnectionError,
                BrokenPipeError,
                ConnectionResetError,
            ),
        ):
            return True

        error_text = str(error).lower()
        retryable_markers = (
            "socket is not connected",
            "must be online session",
            "недопустимое состояние сессии",
            "broken pipe",
            "connection reset",
            "no route to host",
            "network is unreachable",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "tlsv1 alert",
            "ssl:",
        )
        return any(marker in error_text for marker in retryable_markers)

    def _cleanup_pending_state(self):
        now = time.monotonic()
        self._pending_outbound_acks = [
            pending
            for pending in self._pending_outbound_acks
            if now - pending.created_monotonic <= 30
        ]
        self._expected_outbound_ids = {
            key: expires_at
            for key, expires_at in self._expected_outbound_ids.items()
            if expires_at > now
        }

    def _remember_expected_outbound_id(self, chat_id: str, msg_id: str):
        self._cleanup_pending_state()
        self._expected_outbound_ids[(str(chat_id), str(msg_id))] = time.monotonic() + 30

    def _consume_expected_outbound_id(self, chat_id: str, msg_id: str) -> bool:
        self._cleanup_pending_state()
        key = (str(chat_id), str(msg_id))
        expires_at = self._expected_outbound_ids.pop(key, None)
        return expires_at is not None

    def _claim_pending_outbound_ack(self, chat_id: str, text: Optional[str],
                                    reply_to_msg_id: Optional[str]) -> Optional[PendingOutboundAck]:
        self._cleanup_pending_state()
        normalized = self._normalize_outbound_text(text)
        if not normalized:
            return None

        for pending in list(self._pending_outbound_acks):
            if pending.chat_id != str(chat_id):
                continue
            if pending.text != normalized:
                continue
            if pending.reply_to_msg_id and reply_to_msg_id and pending.reply_to_msg_id != reply_to_msg_id:
                continue
            self._pending_outbound_acks.remove(pending)
            return pending
        return None

    def _extract_result_msg_id(self, result) -> Optional[str]:
        if result is None:
            return None

        direct_id = getattr(result, "id", None) or getattr(result, "message_id", None)
        if direct_id is not None:
            return str(direct_id)

        def from_dict(data) -> Optional[str]:
            if not isinstance(data, dict):
                return None
            for key in ("id", "messageId", "message_id"):
                if data.get(key) is not None:
                    return str(data[key])
            for key in ("message", "payload", "result", "msg"):
                nested = data.get(key)
                found = from_dict(nested)
                if found:
                    return found
            return None

        return from_dict(result)

    def _extract_reply_to_msg_id(self, message) -> Optional[str]:
        link = getattr(message, "link", None)
        if not link:
            return None

        link_type = str(getattr(link, "type", "") or "").upper()
        if link_type and link_type != "REPLY":
            return None

        linked_msg = getattr(link, "message", None)
        linked_id = getattr(linked_msg, "id", None) if linked_msg else None
        if linked_id is None:
            linked_id = getattr(link, "message_id", None)
        return str(linked_id) if linked_id is not None else None

    def _extract_forwarded_payload(self, message) -> Optional[ForwardedPayload]:
        """Вернуть вложенное MAX-сообщение для forward/channel link.

        В MAX пересланные сообщения и посты каналов могут приходить как обычное
        сообщение-обёртка с `link.message`. `REPLY` оставляем reply, всё
        остальное с вложенным message разворачиваем как реальный контент.
        """
        link = getattr(message, "link", None)
        if link:
            link_type = str(getattr(link, "type", "") or "").upper() or None
            linked_message = getattr(link, "message", None)
            if linked_message is not None and link_type != "REPLY":
                linked_id = getattr(linked_message, "id", None)
                return ForwardedPayload(
                    message=linked_message,
                    chat_id=str(getattr(link, "chat_id", "") or "") or None,
                    msg_id=str(linked_id) if linked_id is not None else None,
                    link_type=link_type,
                )

        for attr in (
            "forwarded_message",
            "forward_message",
            "forwardedMessage",
            "forwardMessage",
            "channel_message",
            "channelMessage",
        ):
            linked_message = getattr(message, attr, None)
            if linked_message is None:
                continue
            linked_chat_id = (
                getattr(linked_message, "chat_id", None)
                or getattr(message, "_forward_source_chat_id", None)
            )
            linked_id = getattr(linked_message, "id", None)
            return ForwardedPayload(
                message=linked_message,
                chat_id=str(linked_chat_id) if linked_chat_id is not None else None,
                msg_id=str(linked_id) if linked_id is not None else None,
                link_type=attr,
            )

        return None

    def _object_field_names(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, dict):
            return sorted(str(key) for key in value if not str(key).startswith("_"))
        raw_fields = getattr(value, "__dict__", None)
        if isinstance(raw_fields, dict):
            return sorted(str(key) for key in raw_fields if not str(key).startswith("_"))
        return []

    def _object_text_len(self, value) -> Optional[int]:
        text = getattr(value, "text", None)
        return len(text) if isinstance(text, str) else None

    def _object_attach_count(self, value) -> Optional[int]:
        attaches = getattr(value, "attaches", None)
        if attaches is None:
            return None
        if isinstance(attaches, list):
            return len(attaches)
        return 1

    def _safe_message_structure_summary(self, value) -> dict[str, object]:
        if value is None:
            return {}

        def field(source, *names: str):
            if isinstance(source, dict):
                return self._payload_value(source, *names)
            for name in names:
                if hasattr(source, name):
                    return getattr(source, name, None)
            return None

        summary: dict[str, object] = {}
        elements = field(value, "elements") or []
        if isinstance(elements, list):
            summary["element_count"] = len(elements)
            element_types: list[str] = []
            element_classes: list[str] = []
            element_fields: set[str] = set()
            for element in elements[:10]:
                if element is None:
                    continue
                element_classes.append(element.__class__.__name__)
                element_type = field(element, "type")
                if element_type is not None:
                    element_types.append(str(getattr(element_type, "value", element_type)))
                element_fields.update(self._object_field_names(element))
            if element_types:
                summary["element_types"] = sorted(dict.fromkeys(element_types))
            if element_classes:
                summary["element_classes"] = sorted(dict.fromkeys(element_classes))
            if element_fields:
                summary["element_fields"] = sorted(element_fields)
        elif elements is not None:
            summary["element_count"] = 1
            summary["element_class"] = elements.__class__.__name__
            element_type = field(elements, "type")
            if element_type is not None:
                summary["element_types"] = [str(getattr(element_type, "value", element_type))]
            element_fields = self._object_field_names(elements)
            if element_fields:
                summary["element_fields"] = element_fields

        options = field(value, "options")
        if isinstance(options, dict):
            summary["options_class"] = options.__class__.__name__
            summary["options_fields"] = self._safe_field_paths(options, max_depth=1)
        elif isinstance(options, list):
            summary["options_class"] = options.__class__.__name__
            summary["options_count"] = len(options)
            option_classes = [
                option.__class__.__name__
                for option in options[:10]
                if option is not None
            ]
            if option_classes:
                summary["option_classes"] = sorted(dict.fromkeys(option_classes))
        elif options is not None:
            summary["options_class"] = options.__class__.__name__

        return summary

    def _render_unknown_message_details(
        self,
        *,
        message,
        content_message,
        message_type: Optional[str],
        status: Optional[str],
        raw_attachment_types: list[str],
        forwarded: Optional[ForwardedPayload],
    ) -> str:
        details: list[tuple[str, object]] = [
            ("type", message_type or "unknown"),
            ("status", status),
            ("outer_text_len", self._object_text_len(message)),
            ("content_text_len", self._object_text_len(content_message)),
            ("outer_attach_count", self._object_attach_count(message)),
            ("content_attach_count", self._object_attach_count(content_message)),
        ]

        link = getattr(message, "link", None)
        if forwarded:
            details.extend([
                ("link_type", forwarded.link_type),
                ("link_chat_id", forwarded.chat_id),
                ("link_message_id", forwarded.msg_id),
            ])
        elif link:
            linked_message = getattr(link, "message", None)
            details.extend([
                ("link_type", getattr(link, "type", None)),
                ("link_chat_id", getattr(link, "chat_id", None)),
                ("link_message_id", getattr(linked_message, "id", None)),
            ])

        if raw_attachment_types:
            details.append(("raw_attachment_types", ",".join(raw_attachment_types)))

        outer_fields = self._object_field_names(message)
        content_fields = self._object_field_names(content_message)
        if outer_fields:
            details.append(("outer_fields", ",".join(outer_fields)))
        if content_fields and content_fields != outer_fields:
            details.append(("content_fields", ",".join(content_fields)))

        lines = ["[Неизвестное сообщение MAX]"]
        for key, value in details:
            if value is None or value == "":
                continue
            lines.append(f"{key}={value}")
        return "\n".join(lines)

    def _cleanup_raw_unwrapped_state(self):
        now = time.monotonic()
        self._raw_unwrapped_message_ids = {
            key: expires_at
            for key, expires_at in self._raw_unwrapped_message_ids.items()
            if expires_at > now
        }
        self._raw_processed_message_ids = {
            key: expires_at
            for key, expires_at in self._raw_processed_message_ids.items()
            if expires_at > now
        }
        self._raw_history_messages = {
            key: value
            for key, value in self._raw_history_messages.items()
            if value[0] > now
        }
        self._expected_raw_history_messages = {
            msg_id: value
            for msg_id, value in self._expected_raw_history_messages.items()
            if value[1] > now
        }

    def _remember_expected_raw_history_message(self, chat_id: str, msg_id: str):
        self._cleanup_raw_unwrapped_state()
        self._expected_raw_history_messages[str(msg_id)] = (
            str(chat_id),
            time.monotonic() + MAX_RAW_HISTORY_EXPECTED_TTL_SECONDS,
        )

    def _expected_raw_history_chat_id(self, msg_id: object) -> Optional[str]:
        if msg_id is None:
            return None
        self._cleanup_raw_unwrapped_state()
        expected = self._expected_raw_history_messages.get(str(msg_id))
        if expected is None:
            return None
        return expected[0]

    def _mark_raw_unwrapped_message(self, chat_id: str, msg_id: str):
        self._cleanup_raw_unwrapped_state()
        self._raw_unwrapped_message_ids[(str(chat_id), str(msg_id))] = (
            time.monotonic() + 30
        )

    def _consume_raw_unwrapped_message(self, chat_id: str, msg_id: str) -> bool:
        self._cleanup_raw_unwrapped_state()
        return (
            self._raw_unwrapped_message_ids.pop((str(chat_id), str(msg_id)), None)
            is not None
        )

    def _mark_raw_processed_message(self, chat_id: str, msg_id: str):
        self._cleanup_raw_unwrapped_state()
        self._raw_processed_message_ids[(str(chat_id), str(msg_id))] = (
            time.monotonic() + 30
        )

    def _is_raw_processed_message(self, chat_id: str, msg_id: str) -> bool:
        self._cleanup_raw_unwrapped_state()
        return (str(chat_id), str(msg_id)) in self._raw_processed_message_ids

    def _payload_value(self, data: dict, *keys: str):
        normalized = {
            str(k).lower().replace("_", ""): v
            for k, v in data.items()
        }
        for key in keys:
            candidate = key.lower().replace("_", "")
            if candidate in normalized:
                return normalized[candidate]
        return None

    def _raw_opcode_name(self, opcode) -> Optional[str]:
        opcode_value = getattr(opcode, "value", opcode)
        try:
            from pymax.static.enum import Opcode

            return Opcode(opcode_value).name
        except Exception:
            return str(getattr(opcode, "name", "") or "") or None

    def _is_safe_field_name(self, name: object) -> bool:
        lowered = str(name).lower()
        blocked = ("url", "token", "text", "raw")
        return not any(marker in lowered for marker in blocked)

    def _safe_field_paths(self, value, *, max_depth: int = 2, max_items: int = 80) -> list[str]:
        paths: list[str] = []
        seen: set[int] = set()

        def iter_items(node):
            if isinstance(node, dict):
                return node.items()
            raw_fields = getattr(node, "__dict__", None)
            if isinstance(raw_fields, dict):
                return raw_fields.items()
            return ()

        def walk(node, prefix: str, depth: int):
            if node is None or depth > max_depth or len(paths) >= max_items:
                return
            if isinstance(node, (str, bytes, int, float, bool)):
                return
            if isinstance(node, (dict, list, tuple, set)) or hasattr(node, "__dict__"):
                node_id = id(node)
                if node_id in seen:
                    return
                seen.add(node_id)

            if isinstance(node, (list, tuple, set)):
                if prefix:
                    list_path = f"{prefix}[]"
                    if list_path not in paths:
                        paths.append(list_path)
                for item in list(node)[:5]:
                    walk(item, f"{prefix}[]" if prefix else "[]", depth + 1)
                return

            for key, child in iter_items(node):
                name = str(key)
                if name.startswith("_") or not self._is_safe_field_name(name):
                    continue
                path = f"{prefix}.{name}" if prefix else name
                paths.append(path)
                if len(paths) >= max_items:
                    return
                walk(child, path, depth + 1)

        walk(value, "", 0)
        return sorted(dict.fromkeys(paths))

    def _normalize_message_dict(self, data: dict) -> dict:
        normalized = dict(data)
        if "_type" in normalized and "type" not in normalized:
            normalized["type"] = normalized["_type"]
        if "chat_id" in normalized and "chatId" not in normalized:
            normalized["chatId"] = normalized["chat_id"]
        if "chatId" in normalized and "chat_id" not in normalized:
            normalized["chat_id"] = normalized["chatId"]
        if "message_id" in normalized and "id" not in normalized:
            normalized["id"] = normalized["message_id"]
        if "messageId" in normalized and "id" not in normalized:
            normalized["id"] = normalized["messageId"]
        if "msgId" in normalized and "id" not in normalized:
            normalized["id"] = normalized["msgId"]
        if "attachments" in normalized and "attaches" not in normalized:
            normalized["attaches"] = normalized["attachments"]
        for source, target in {
            "baseUrl": "base_url",
            "fileId": "file_id",
            "videoId": "video_id",
            "audioId": "audio_id",
        }.items():
            if source in normalized and target not in normalized:
                normalized[target] = normalized[source]
        return self._normalize_raw_media_fields(normalized)

    def _normalize_raw_media_fields(self, message: dict) -> dict:
        if not isinstance(message, dict):
            return message
        if not any(key in message for key in ("id", "messageId", "message_id", "msgId", "sender", "text", "attaches", "attachments")):
            return message

        def infer_media_type_for_key(key: str, node: dict) -> Optional[str]:
            raw_type = self._payload_value(node, "_type", "type", "mediaType", "kind")
            if raw_type:
                upper = str(getattr(raw_type, "value", raw_type)).upper()
                if "VOICE" in upper or "AUDIO" in upper:
                    return "AUDIO"
                if "VIDEO" in upper:
                    return "VIDEO"
                if "PHOTO" in upper or "IMAGE" in upper:
                    return "PHOTO"
                if "FILE" in upper or "DOCUMENT" in upper:
                    return "FILE"
            if self._payload_value(node, "audioId", "audio_id", "wave") is not None:
                return "AUDIO"
            if self._payload_value(node, "videoId", "video_id") is not None:
                return "VIDEO"
            if self._payload_value(node, "photoId", "photo_id", "imageId", "image_id") is not None:
                return "PHOTO"
            key_lower = key.lower()
            if "voice" in key_lower or "audio" in key_lower:
                return "AUDIO"
            if "video" in key_lower:
                return "VIDEO"
            if "photo" in key_lower or "image" in key_lower:
                return "PHOTO"
            return None

        def copy_nested_media_markers(attach: dict) -> dict:
            normalized_attach = dict(attach)
            marker_keys = (
                "audioId",
                "audio_id",
                "videoId",
                "video_id",
                "photoId",
                "photo_id",
                "imageId",
                "image_id",
                "fileId",
                "file_id",
                "url",
                "baseUrl",
                "duration",
                "wave",
            )
            for nested_key in (
                "audio",
                "voice",
                "audioMessage",
                "voiceMessage",
                "media",
                "file",
                "payload",
                "data",
                "content",
                "body",
            ):
                nested = self._payload_value(normalized_attach, nested_key)
                if not isinstance(nested, dict):
                    continue
                for marker in marker_keys:
                    if self._payload_value(normalized_attach, marker) is not None:
                        continue
                    value = self._payload_value(nested, marker)
                    if value is not None:
                        normalized_attach[marker] = value
            return normalized_attach

        existing = self._payload_value(message, "attaches", "attachments") or []
        if existing:
            existing_list = existing if isinstance(existing, list) else [existing]
            normalized_attaches: list[object] = []
            changed = False
            for attach in existing_list:
                if not isinstance(attach, dict):
                    normalized_attaches.append(attach)
                    continue
                normalized_attach = copy_nested_media_markers(attach)
                raw_type = self._payload_value(normalized_attach, "_type", "type")
                upper_type = str(getattr(raw_type, "value", raw_type) or "").upper()
                inferred_type = infer_media_type_for_key("attach", normalized_attach)
                if inferred_type:
                    normalized_attach["_type"] = inferred_type
                    normalized_attach["type"] = inferred_type
                    changed = True
                elif upper_type:
                    normalized_attach["_type"] = upper_type
                    normalized_attach["type"] = upper_type
                if normalized_attach != attach:
                    changed = True
                normalized_attaches.append(normalized_attach)
            if changed:
                normalized = dict(message)
                normalized["attaches"] = normalized_attaches
                normalized.setdefault("attachments", normalized_attaches)
                return normalized
            return message

        def media_type_for_key(key: str, node: dict) -> Optional[str]:
            return infer_media_type_for_key(key, node)

        def looks_like_media(node: dict) -> bool:
            media_markers = (
                "audioId",
                "audio_id",
                "videoId",
                "video_id",
                "fileId",
                "file_id",
                "photoId",
                "photo_id",
                "url",
                "baseUrl",
                "duration",
                "wave",
            )
            return any(self._payload_value(node, marker) is not None for marker in media_markers)

        top_level_type = media_type_for_key("message", message)
        if top_level_type and looks_like_media(message):
            attach = dict(message)
            attach["_type"] = top_level_type
            attach["type"] = top_level_type
            normalized = dict(message)
            normalized["attaches"] = [attach]
            normalized.setdefault("attachments", [attach])
            return normalized

        media_container_keys = (
            "audio",
            "voice",
            "audioMessage",
            "voiceMessage",
            "audios",
            "voices",
            "media",
            "medias",
            "attachment",
            "attachments",
            "attach",
            "attaches",
            "file",
            "files",
            "video",
            "videos",
            "photo",
            "photos",
            "image",
            "images",
            "content",
            "body",
            "data",
            "payload",
            "object",
            "item",
            "items",
            "parts",
            "elements",
        )

        candidates: list[dict] = []

        def collect(key: str, node):
            if node is None:
                return
            if isinstance(node, list):
                for item in node:
                    collect(key, item)
                return
            if not isinstance(node, dict):
                return
            attach_type = media_type_for_key(key, node)
            if attach_type and looks_like_media(node):
                attach = dict(node)
                attach["_type"] = attach_type
                attach["type"] = attach_type
                candidates.append(attach)
                return
            for nested_key in media_container_keys:
                nested = self._payload_value(node, nested_key)
                if nested is not None:
                    collect(nested_key, nested)

        for key in media_container_keys:
            collect(key, self._payload_value(message, key))

        if candidates:
            normalized = dict(message)
            normalized["attaches"] = candidates
            normalized.setdefault("attachments", candidates)
            return normalized
        return message

    def _message_dict_has_content(self, message: dict) -> bool:
        normalized = self._normalize_raw_media_fields(message)
        text = self._payload_value(normalized, "text")
        attaches = self._payload_value(normalized, "attaches", "attachments") or []
        return bool((text or "").strip() or attaches)

    def _message_object_has_content(self, message) -> bool:
        text = getattr(message, "text", None)
        if isinstance(text, str) and text.strip():
            return True
        if text and not isinstance(text, str):
            return True

        attaches = getattr(message, "attaches", None) or []
        if isinstance(attaches, list):
            return any(attach is not None for attach in attaches)
        return attaches is not None

    def _raw_attachment_types_from_message_dict(self, message: dict) -> list[str]:
        attaches = self._payload_value(message, "attaches", "attachments") or []
        if not isinstance(attaches, list):
            attaches = [attaches]
        types: list[str] = []
        for attach in attaches:
            if not isinstance(attach, dict):
                continue
            raw_type = self._payload_value(attach, "type", "_type")
            if raw_type:
                types.append(str(raw_type).upper())
        return types

    def _payload_message_dict(self, payload: dict) -> tuple[Optional[dict], object]:
        if not isinstance(payload, dict):
            return None, None

        outer_chat_id = self._payload_value(payload, "chatId", "chat_id")
        message = self._payload_value(payload, "message")
        if isinstance(message, dict):
            return self._normalize_message_dict(message), outer_chat_id

        # Some MAX DM voice notifications arrive as a message-shaped payload
        # directly, not as {"chatId": ..., "message": {...}}. pymax then misses
        # aliases like "attachments" and emits an empty typed USER event.
        message_shaped_keys = (
            "id",
            "messageId",
            "message_id",
            "text",
            "attaches",
            "attachments",
            "type",
            "_type",
        )
        if any(self._payload_value(payload, key) is not None for key in message_shaped_keys):
            return self._normalize_message_dict(payload), outer_chat_id

        return None, outer_chat_id

    def _raw_payload_message_identity(self, payload: dict) -> tuple[str, str] | None:
        message, outer_chat_id = self._payload_message_dict(payload)
        if not message:
            return None
        chat_id = self._payload_value(message, "chatId", "chat_id") or outer_chat_id
        msg_id = self._payload_value(message, "id", "messageId", "message_id", "msgId")
        if chat_id is None or msg_id is None:
            return None
        return str(chat_id), str(msg_id)

    def _find_nested_message_dict(self, wrapper: dict) -> tuple[Optional[dict], Optional[str]]:
        for key in (
            "message",
            "forwardedMessage",
            "forwardMessage",
            "channelMessage",
            "sourceMessage",
            "originalMessage",
        ):
            value = self._payload_value(wrapper, key)
            if not isinstance(value, dict):
                continue
            source_chat_id = self._payload_value(value, "chatId", "chat_id")
            nested = self._payload_value(value, "message")
            if isinstance(nested, dict):
                return self._normalize_message_dict(nested), (
                    str(source_chat_id) if source_chat_id is not None else None
                )
            return self._normalize_message_dict(value), (
                str(source_chat_id) if source_chat_id is not None else None
            )
        return None, None

    def _message_object_from_dict(
        self,
        message: dict,
        chat_id: Optional[str],
        *,
        prefer_raw: bool = False,
    ):
        payload = {
            "chatId": (
                int(chat_id)
                if chat_id and str(chat_id).lstrip("-").isdigit()
                else chat_id
            ),
            "message": self._normalize_message_dict(message),
        }
        if not prefer_raw:
            try:
                from pymax.types import Message

                return Message.from_dict(payload)
            except Exception:
                pass
        normalized_message = payload["message"]
        attaches = [
            SimpleNamespace(**self._normalize_message_dict(attach))
            for attach in (normalized_message.get("attaches") or [])
            if isinstance(attach, dict)
        ]
        return SimpleNamespace(
            id=normalized_message.get("id"),
            chat_id=chat_id,
            sender=normalized_message.get("sender"),
            time=normalized_message.get("time"),
            text=normalized_message.get("text") or "",
            type=normalized_message.get("type"),
            status=normalized_message.get("status"),
            attaches=attaches,
            link=None,
            reactionInfo=normalized_message.get("reactionInfo"),
        )

    def _cache_raw_history_payload(self, payload: dict) -> int:
        """Cache raw CHAT_HISTORY messages briefly for empty pymax events."""
        if not isinstance(payload, dict):
            return 0

        raw_messages = self._payload_value(payload, "messages")
        if not isinstance(raw_messages, list):
            return 0

        self._cleanup_raw_unwrapped_state()
        outer_chat_id = self._payload_value(payload, "chatId", "chat_id")
        cached = 0
        now = time.monotonic()

        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                continue
            message = self._normalize_message_dict(raw_message)
            if not self._message_dict_has_content(message):
                continue

            msg_id = self._payload_value(message, "id", "messageId", "message_id", "msgId")
            chat_id = (
                self._payload_value(message, "chatId", "chat_id")
                or outer_chat_id
                or self._expected_raw_history_chat_id(msg_id)
            )
            if chat_id is None or msg_id is None:
                continue
            if is_probable_client_cid(chat_id):
                continue

            message_obj = self._message_object_from_dict(
                message,
                str(chat_id),
                prefer_raw=True,
            )
            self._raw_history_messages[(str(chat_id), str(msg_id))] = (
                now + MAX_RAW_HISTORY_CACHE_TTL_SECONDS,
                message_obj,
            )
            cached += 1

        if len(self._raw_history_messages) > MAX_RAW_HISTORY_CACHE_SIZE:
            newest = sorted(
                self._raw_history_messages.items(),
                key=lambda item: item[1][0],
                reverse=True,
            )[:MAX_RAW_HISTORY_CACHE_SIZE]
            self._raw_history_messages = dict(newest)

        return cached

    def _get_cached_raw_history_message(self, chat_id: str, msg_id: str):
        self._cleanup_raw_unwrapped_state()
        cached = self._raw_history_messages.get((str(chat_id), str(msg_id)))
        if cached is None:
            return None
        _expires_at, message = cached
        return message

    def _raw_history_message_dicts(self, payload: dict) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        raw_messages = self._payload_value(payload, "messages")
        if not isinstance(raw_messages, list):
            return []
        return [
            self._normalize_message_dict(raw_message)
            for raw_message in raw_messages
            if isinstance(raw_message, dict)
        ]

    def _find_raw_history_message_dict(self, payload: dict, msg_id: str) -> Optional[dict]:
        msg_id_str = str(msg_id)
        for message in self._raw_history_message_dicts(payload):
            candidate_id = self._payload_value(
                message,
                "id",
                "messageId",
                "message_id",
                "msgId",
            )
            if str(candidate_id) == msg_id_str:
                return message
        return None

    async def _fetch_raw_history_payload(
        self,
        *,
        chat_id_int: int,
        from_time: int,
        forward: int,
        backward: int,
        flow_id: Optional[str] = None,
    ) -> Optional[dict]:
        if not self._client or getattr(self._client, "_send_and_wait", None) is None:
            return None
        try:
            from pymax.payloads import FetchHistoryPayload
            from pymax.static.enum import Opcode

            payload = FetchHistoryPayload(
                chat_id=chat_id_int,
                from_time=from_time,
                forward=forward,
                backward=backward,
            ).model_dump(by_alias=True)
            data = await self._client._send_and_wait(
                opcode=Opcode.CHAT_HISTORY,
                payload=payload,
                timeout=10,
            )
        except Exception as e:
            log_event(
                logger,
                logging.INFO,
                "max.raw.history_fetch",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="failed",
                reason="raw_history_failed",
                max_chat_id=str(chat_id_int),
                error=str(e),
            )
            return None

        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return None
        cached = self._cache_raw_history_payload(payload)
        log_event(
            logger,
            logging.INFO,
            "max.raw.history_fetch",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="received",
            max_chat_id=str(chat_id_int),
            message_count=len(self._raw_history_message_dicts(payload)),
            cached_count=cached,
        )
        return payload

    def _prepare_empty_recovery_candidate(
        self,
        candidate,
        *,
        chat_id: str,
        chat_id_int: int,
        raw_msg_id_str: str,
        flow_id: str,
        reason: str,
    ):
        if isinstance(candidate, dict):
            candidate = self._message_object_from_dict(
                self._normalize_message_dict(candidate),
                chat_id,
                prefer_raw=True,
            )

        if not self._message_object_has_content(candidate):
            log_event(
                logger,
                logging.INFO,
                "max.inbound.empty_recovery",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="skipped",
                reason=f"{reason}_without_content",
                max_chat_id=chat_id,
                max_msg_id=raw_msg_id_str,
                message_class=candidate.__class__.__name__,
                message_fields=self._safe_attachment_field_names(candidate),
                **self._safe_message_structure_summary(candidate),
            )
            return None

        setattr(candidate, "_from_empty_recovery", True)
        candidate_chat_id = getattr(candidate, "chat_id", None)
        if candidate_chat_id is None:
            setattr(candidate, "chat_id", chat_id_int)
        attaches = getattr(candidate, "attaches", None) or []
        attach_list = attaches if isinstance(attaches, list) else [attaches]
        attachment_types = [
            self._normalize_attachment_type(self._attachment_type_name(attach))
            for attach in attach_list
            if attach is not None and self._attachment_type_name(attach)
        ]
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="recovered",
            reason=reason,
            max_chat_id=chat_id,
            max_msg_id=raw_msg_id_str,
            attachment_types=attachment_types,
            has_text=bool((getattr(candidate, "text", None) or "").strip()),
        )
        return candidate

    def _build_unwrapped_channel_message(self, payload: dict):
        if not isinstance(payload, dict):
            return None

        outer_chat_id = self._payload_value(payload, "chatId", "chat_id")
        wrapper = self._payload_value(payload, "message")
        if not isinstance(wrapper, dict):
            return None

        wrapper = self._normalize_message_dict(wrapper)
        nested, nested_chat_id = self._find_nested_message_dict(wrapper)
        if not nested:
            return None

        wrapper_type = str(self._payload_value(wrapper, "type") or "").upper()
        wrapper_has_content = bool(
            (self._payload_value(wrapper, "text") or "").strip()
            or self._payload_value(wrapper, "attaches")
        )
        nested_has_content = bool(
            (self._payload_value(nested, "text") or "").strip()
            or self._payload_value(nested, "attaches")
        )
        if wrapper_type not in {"CHANNEL", "FORWARD", "FORWARDED"} and (
            wrapper_has_content or not nested_has_content
        ):
            return None

        source_chat_id = (
            nested_chat_id
            or self._payload_value(nested, "chatId", "chat_id")
            or self._payload_value(wrapper, "chatId", "chat_id")
            or outer_chat_id
        )
        nested_msg_id = self._payload_value(nested, "id", "messageId", "message_id")
        outer_msg_id = (
            self._payload_value(wrapper, "id", "messageId", "message_id")
            or nested_msg_id
        )
        outer_status = self._payload_value(wrapper, "status")
        nested_obj = self._message_object_from_dict(
            nested,
            str(source_chat_id) if source_chat_id else None,
        )

        return SimpleNamespace(
            id=outer_msg_id,
            chat_id=outer_chat_id or source_chat_id,
            sender=(
                self._payload_value(wrapper, "sender")
                or getattr(nested_obj, "sender", None)
            ),
            text=getattr(nested_obj, "text", None),
            type=getattr(nested_obj, "type", None),
            status=outer_status or getattr(nested_obj, "status", None),
            attaches=getattr(nested_obj, "attaches", None),
            link=None,
            reactionInfo=(
                self._payload_value(wrapper, "reactionInfo")
                or getattr(nested_obj, "reactionInfo", None)
            ),
            _forward_source_chat_id=(
                str(source_chat_id) if source_chat_id is not None else None
            ),
            _forward_source_msg_id=(
                str(nested_msg_id) if nested_msg_id is not None else None
            ),
            _forward_link_type=wrapper_type or "CHANNEL",
            _from_raw_unwrapped=True,
        )

    def _build_raw_regular_message(self, payload: dict):
        message, outer_chat_id = self._payload_message_dict(payload)
        if not message:
            return None

        message_type = str(self._payload_value(message, "type") or "").upper()
        if message_type in {"CHANNEL", "FORWARD", "FORWARDED"}:
            return None
        if not self._message_dict_has_content(message):
            return None

        chat_id = (
            self._payload_value(message, "chatId", "chat_id")
            or outer_chat_id
        )
        if chat_id is None or is_probable_client_cid(chat_id):
            return None
        message_obj = self._message_object_from_dict(
            message,
            str(chat_id),
            prefer_raw=True,
        )
        setattr(message_obj, "_from_raw_unwrapped", True)
        return message_obj

    def _log_raw_message_missing_chat_id(self, payload: dict):
        message, _outer_chat_id = self._payload_message_dict(payload)
        if not message or not self._message_dict_has_content(message):
            return

        msg_id = self._payload_value(message, "id", "messageId", "message_id", "msgId")
        flow_id = build_max_flow_id("", str(msg_id or ""))
        log_event(
            logger,
            logging.INFO,
            "max.raw.message_skipped",
            flow_id=flow_id,
            direction="inbound",
            stage="received",
            outcome="skipped",
            reason="missing_chat_id",
            max_chat_id=None,
            max_msg_id=str(msg_id) if msg_id is not None else None,
            message_type=str(self._payload_value(message, "type") or "") or None,
            payload_fields=self._safe_attachment_field_names(SimpleNamespace(**payload)),
            message_fields=self._safe_attachment_field_names(SimpleNamespace(**message)),
            raw_attachment_types=self._raw_attachment_types_from_message_dict(message),
        )

    def _log_raw_empty_message(self, payload: dict):
        message, outer_chat_id = self._payload_message_dict(payload)
        if not message:
            return

        if self._message_dict_has_content(message):
            return

        message_type = str(self._payload_value(message, "type") or "").upper()
        if message_type not in {"", "TEXT", "USER"}:
            return

        msg_id = self._payload_value(message, "id", "messageId", "message_id")
        flow_id = build_max_flow_id(str(outer_chat_id or ""), str(msg_id or ""))
        log_event(
            logger,
            logging.INFO,
            "max.raw.empty_message",
            flow_id=flow_id,
            direction="inbound",
            stage="received",
            outcome="diagnostic",
            reason="raw_message_without_content",
            max_chat_id=str(outer_chat_id) if outer_chat_id is not None else None,
            max_msg_id=str(msg_id) if msg_id is not None else None,
            message_type=message_type or None,
            payload_fields=self._safe_attachment_field_names(SimpleNamespace(**payload)),
            message_fields=self._safe_attachment_field_names(SimpleNamespace(**message)),
            raw_attachment_types=self._raw_attachment_types_from_message_dict(message),
        )

    def _raw_payload_identity_hints(self, payload: dict) -> tuple[object, object]:
        message, outer_chat_id = self._payload_message_dict(payload)
        if message:
            msg_id = self._payload_value(message, "id", "messageId", "message_id", "msgId")
            chat_id = (
                self._payload_value(message, "chatId", "chat_id")
                or outer_chat_id
                or self._expected_raw_history_chat_id(msg_id)
            )
            return chat_id, msg_id

        messages = self._payload_value(payload, "messages")
        if isinstance(messages, list):
            for raw_message in messages:
                if not isinstance(raw_message, dict):
                    continue
                message = self._normalize_message_dict(raw_message)
                msg_id = self._payload_value(
                    message,
                    "id",
                    "messageId",
                    "message_id",
                    "msgId",
                )
                chat_id = (
                    self._payload_value(message, "chatId", "chat_id")
                    or self._expected_raw_history_chat_id(msg_id)
                )
                if chat_id is not None or msg_id is not None:
                    return chat_id, msg_id

        chat_id = self._payload_value(payload, "chatId", "chat_id")
        msg_id = self._payload_value(payload, "messageId", "message_id", "msgId", "id")
        return chat_id, msg_id

    def _log_raw_unhandled_message_payload(self, payload: dict):
        if not isinstance(payload, dict):
            return

        chat_id, msg_id = self._raw_payload_identity_hints(payload)
        flow_id = build_max_flow_id(str(chat_id or ""), str(msg_id or ""))
        log_event(
            logger,
            logging.INFO,
            "max.raw.unhandled_message_payload",
            flow_id=flow_id,
            direction="inbound",
            stage="received",
            outcome="diagnostic",
            reason="message_payload_shape_unknown",
            max_chat_id=str(chat_id) if chat_id is not None else None,
            max_msg_id=str(msg_id) if msg_id is not None else None,
            payload_class=payload.__class__.__name__,
            payload_fields=self._safe_attachment_field_names(SimpleNamespace(**payload)),
            payload_shape=self._safe_field_paths(payload),
        )

    def _log_raw_auxiliary_event(self, data: dict):
        if not isinstance(data, dict):
            return

        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            return

        raw_opcode = data.get("opcode")
        opcode_value = getattr(raw_opcode, "value", raw_opcode)
        opcode_name = self._raw_opcode_name(raw_opcode)
        payload_shape = self._safe_field_paths(payload)
        interesting_opcode_names = {
            "NOTIF_ATTACH",
            "NOTIF_MSG_DELAYED",
            "NOTIF_DRAFT",
            "NOTIF_DRAFT_DISCARD",
        }
        interesting_field_markers = ("attach", "audio", "voice")
        has_interesting_shape = any(
            any(marker in field.lower() for marker in interesting_field_markers)
            for field in payload_shape
        )
        if opcode_name not in interesting_opcode_names and not has_interesting_shape:
            return

        chat_id, msg_id = self._raw_payload_identity_hints(payload)
        flow_id = build_max_flow_id(str(chat_id or ""), str(msg_id or ""))
        log_event(
            logger,
            logging.INFO,
            "max.raw.auxiliary_event",
            flow_id=flow_id,
            direction="inbound",
            stage="received",
            outcome="diagnostic",
            reason="non_message_notification",
            opcode=opcode_value,
            opcode_name=opcode_name,
            max_chat_id=str(chat_id) if chat_id is not None else None,
            max_msg_id=str(msg_id) if msg_id is not None else None,
            payload_class=payload.__class__.__name__,
            payload_fields=self._safe_attachment_field_names(SimpleNamespace(**payload)),
            payload_shape=payload_shape,
        )

    def _log_typed_empty_message(
        self,
        *,
        flow_id: str,
        message,
        content_message,
        chat_id: str,
        msg_id: str,
        message_type: Optional[str],
        reaction_info,
    ):
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_message",
            flow_id=flow_id,
            direction="inbound",
            stage="normalize",
            outcome="diagnostic",
            reason="typed_message_without_content",
            max_chat_id=chat_id,
            max_msg_id=msg_id,
            message_type=message_type,
            has_reaction_info=bool(reaction_info),
            message_class=message.__class__.__name__,
            content_class=content_message.__class__.__name__,
            message_fields=self._safe_attachment_field_names(message),
            content_fields=self._safe_attachment_field_names(content_message),
            **self._safe_message_structure_summary(content_message),
        )

    async def _recover_empty_message_from_recent_history(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        flow_id: str,
    ):
        try:
            chat_id_int = int(chat_id)
            raw_msg_id_str = str(raw_msg_id)
        except (TypeError, ValueError):
            return None

        cached = self._get_cached_raw_history_message(chat_id, raw_msg_id_str)
        if cached is not None:
            recovered = self._prepare_empty_recovery_candidate(
                cached,
                chat_id=chat_id,
                chat_id_int=chat_id_int,
                raw_msg_id_str=raw_msg_id_str,
                flow_id=flow_id,
                reason="raw_history_cache_match",
            )
            if recovered is not None:
                return recovered

        if not self._client:
            return None

        self._remember_expected_raw_history_message(chat_id, raw_msg_id_str)
        history_from_time = int(time.time() * 1000) + 60_000
        raw_payload = await self._fetch_raw_history_payload(
            chat_id_int=chat_id_int,
            from_time=history_from_time,
            forward=0,
            backward=10,
            flow_id=flow_id,
        )
        if raw_payload is not None:
            raw_candidate = self._find_raw_history_message_dict(raw_payload, raw_msg_id_str)
            if raw_candidate is not None:
                recovered = self._prepare_empty_recovery_candidate(
                    raw_candidate,
                    chat_id=chat_id,
                    chat_id_int=chat_id_int,
                    raw_msg_id_str=raw_msg_id_str,
                    flow_id=flow_id,
                    reason="raw_recent_history_match",
                )
                if recovered is not None:
                    return recovered

        if getattr(self._client, "fetch_history", None) is None:
            return None

        try:
            messages = await self._client.fetch_history(
                chat_id_int,
                from_time=history_from_time,
                forward=0,
                backward=10,
            )
        except Exception as e:
            await asyncio.sleep(0.2)
            cached = self._get_cached_raw_history_message(chat_id, raw_msg_id_str)
            if cached is not None:
                recovered = self._prepare_empty_recovery_candidate(
                    cached,
                    chat_id=chat_id,
                    chat_id_int=chat_id_int,
                    raw_msg_id_str=raw_msg_id_str,
                    flow_id=flow_id,
                    reason="raw_history_cache_after_fetch_error",
                )
                if recovered is not None:
                    return recovered

            log_event(
                logger,
                logging.WARNING,
                "max.inbound.empty_recovery",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="failed",
                reason="recent_history_failed",
                max_chat_id=chat_id,
                max_msg_id=raw_msg_id_str,
                error=str(e),
            )
            return None

        for candidate in messages or []:
            if isinstance(candidate, dict):
                candidate_id = self._payload_value(
                    candidate,
                    "id",
                    "messageId",
                    "message_id",
                    "msgId",
                )
            else:
                candidate_id = getattr(candidate, "id", None)
            if str(candidate_id) != raw_msg_id_str:
                continue
            return self._prepare_empty_recovery_candidate(
                candidate,
                chat_id=chat_id,
                chat_id_int=chat_id_int,
                raw_msg_id_str=raw_msg_id_str,
                flow_id=flow_id,
                reason="recent_history_match",
            )

        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="miss",
            reason="recent_history_message_not_found",
            max_chat_id=chat_id,
            max_msg_id=raw_msg_id_str,
        )
        return None

    def get_pending_empty_recovery_stats(self) -> dict[str, Optional[int]]:
        if not self._pending_empty_recoveries:
            return {"pending_count": 0, "oldest_created_at": None}
        created_values = [
            int(job.get("created_at") or 0)
            for job in self._pending_empty_recoveries.values()
            if job.get("created_at")
        ]
        oldest = min(created_values) if created_values else None
        return {
            "pending_count": len(self._pending_empty_recoveries),
            "oldest_created_at": oldest,
        }

    def _history_message_time_seconds(self, message) -> Optional[int]:
        value = (
            self._payload_value(message, "time")
            if isinstance(message, dict)
            else getattr(message, "time", None)
        )
        try:
            ts = int(value)
        except (TypeError, ValueError):
            return None
        if ts > 10_000_000_000:
            return ts // 1000
        return ts

    def _pending_empty_recovery_ids_for_chat(self, chat_id: str) -> set[str]:
        return {
            str(job.get("raw_msg_id"))
            for job in self._pending_empty_recoveries.values()
            if str(job.get("chat_id")) == str(chat_id) and job.get("raw_msg_id") is not None
        }

    def _log_history_sweep_pending_diagnostic(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        reason: str,
        flow_id: Optional[str],
        message: Optional[dict] = None,
        message_count: Optional[int] = None,
    ):
        key = (str(chat_id), str(raw_msg_id), reason)
        now = time.monotonic()
        if self._history_sweep_diagnostic_log_until.get(key, 0) > now:
            return
        self._history_sweep_diagnostic_log_until[key] = (
            now + MAX_HISTORY_SWEEP_DIAGNOSTIC_TTL_SECONDS
        )
        fields: dict[str, object] = {
            "flow_id": flow_id,
            "direction": "inbound",
            "stage": "history_sweep",
            "outcome": "diagnostic",
            "reason": reason,
            "max_chat_id": str(chat_id),
            "max_msg_id": str(raw_msg_id),
            "message_count": message_count,
        }
        if isinstance(message, dict):
            fields.update(
                {
                    "message_type": str(
                        self._payload_value(message, "type", "_type") or ""
                    ) or None,
                    "message_fields": self._safe_field_paths(message),
                    "raw_attachment_types": self._raw_attachment_types_from_message_dict(
                        message
                    ),
                }
            )
        log_event(
            logger,
            logging.INFO,
            "max.history_sweep.pending_diagnostic",
            **fields,
        )

    async def replay_recent_history(
        self,
        chat_id: str,
        *,
        limit: int = 30,
        since_ts: Optional[int] = None,
        flow_id: Optional[str] = None,
    ) -> int:
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return 0
        if is_probable_client_cid(chat_id_int):
            return 0

        from_time = int(time.time() * 1000) + 60_000
        raw_payload = await self._fetch_raw_history_payload(
            chat_id_int=chat_id_int,
            from_time=from_time,
            forward=0,
            backward=max(1, int(limit)),
            flow_id=flow_id,
        )
        candidates: list[object] = []
        if raw_payload is not None:
            pending_ids = self._pending_empty_recovery_ids_for_chat(str(chat_id))
            seen_pending_ids: set[str] = set()
            raw_messages = self._raw_history_message_dicts(raw_payload)
            for message in raw_messages:
                raw_history_msg_id = self._payload_value(
                    message,
                    "id",
                    "messageId",
                    "message_id",
                    "msgId",
                )
                raw_history_msg_id_str = (
                    str(raw_history_msg_id) if raw_history_msg_id is not None else ""
                )
                if raw_history_msg_id_str in pending_ids:
                    seen_pending_ids.add(raw_history_msg_id_str)
                candidate_chat_id = (
                    self._payload_value(message, "chatId", "chat_id")
                    or chat_id
                )
                if is_probable_client_cid(candidate_chat_id):
                    candidate_chat_id = chat_id
                if not self._message_dict_has_content(message):
                    if raw_history_msg_id_str in pending_ids:
                        self._log_history_sweep_pending_diagnostic(
                            chat_id=str(chat_id),
                            raw_msg_id=raw_history_msg_id_str,
                            reason="pending_message_without_content",
                            flow_id=flow_id,
                            message=message,
                            message_count=len(raw_messages),
                        )
                    continue
                candidates.append(
                    self._message_object_from_dict(
                        message,
                        str(candidate_chat_id),
                        prefer_raw=True,
                    )
                )
            for pending_id in pending_ids - seen_pending_ids:
                self._log_history_sweep_pending_diagnostic(
                    chat_id=str(chat_id),
                    raw_msg_id=pending_id,
                    reason="pending_message_not_found",
                    flow_id=flow_id,
                    message_count=len(raw_messages),
                )
        elif self._client and getattr(self._client, "fetch_history", None):
            try:
                candidates = list(
                    await self._client.fetch_history(
                        chat_id_int,
                        from_time=from_time,
                        forward=0,
                        backward=max(1, int(limit)),
                    )
                    or []
                )
            except Exception as e:
                log_event(
                    logger,
                    logging.INFO,
                    "max.history_sweep.fetch_failed",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="history_sweep",
                    outcome="failed",
                    max_chat_id=str(chat_id),
                    error=str(e),
                )
                return 0

        def sort_key(candidate):
            return self._history_message_time_seconds(candidate) or 0

        replayed = 0
        for candidate in sorted(candidates, key=sort_key):
            candidate_ts = self._history_message_time_seconds(candidate)
            if since_ts is not None and candidate_ts is not None and candidate_ts < since_ts:
                continue
            candidate_chat_id = str(getattr(candidate, "chat_id", None) or chat_id)
            if is_probable_client_cid(candidate_chat_id):
                setattr(candidate, "chat_id", chat_id_int)
            if not self._message_object_has_content(candidate):
                continue
            await self._handle_raw_message(candidate)
            replayed += 1

        if replayed:
            log_event(
                logger,
                logging.INFO,
                "max.history_sweep.replayed",
                flow_id=flow_id,
                direction="inbound",
                stage="history_sweep",
                outcome="replayed",
                max_chat_id=str(chat_id),
                replayed_count=replayed,
            )
        return replayed

    def _pending_empty_recovery_path(self) -> Path:
        return Path(self._data_dir) / MAX_EMPTY_RECOVERY_STATE_FILE

    def _pending_empty_recovery_key(self, chat_id: str, raw_msg_id: str) -> str:
        return f"{chat_id}:{raw_msg_id}"

    def _load_pending_empty_recoveries(self):
        path = self._pending_empty_recovery_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.inbound.empty_recovery_state",
                stage="startup",
                outcome="failed",
                reason="load_failed",
                error=str(e),
            )
            return
        if not isinstance(data, list):
            return
        pending: dict[str, dict[str, object]] = {}
        now = int(time.time())
        for item in data:
            if not isinstance(item, dict):
                continue
            chat_id = item.get("chat_id")
            raw_msg_id = item.get("raw_msg_id")
            if chat_id is None or raw_msg_id is None:
                continue
            job = {
                "chat_id": str(chat_id),
                "raw_msg_id": str(raw_msg_id),
                "msg_id": str(item.get("msg_id") or raw_msg_id),
                "message_type": (
                    str(item["message_type"])
                    if item.get("message_type") is not None
                    else None
                ),
                "attempts": int(item.get("attempts") or 0),
                "created_at": int(item.get("created_at") or now),
                "updated_at": int(item.get("updated_at") or now),
                "next_attempt_at": min(
                    int(item.get("next_attempt_at") or now),
                    now + MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS,
                ),
                "last_error": (
                    str(item["last_error"])
                    if item.get("last_error") is not None
                    else None
                ),
            }
            pending[self._pending_empty_recovery_key(str(chat_id), str(raw_msg_id))] = job
        self._pending_empty_recoveries = pending

    def _save_pending_empty_recoveries(self):
        path = self._pending_empty_recovery_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            data = sorted(
                self._pending_empty_recoveries.values(),
                key=lambda item: (
                    int(item.get("next_attempt_at") or 0),
                    str(item.get("chat_id") or ""),
                    str(item.get("raw_msg_id") or ""),
                ),
            )
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.inbound.empty_recovery_state",
                stage="runtime",
                outcome="failed",
                reason="save_failed",
                error=str(e),
            )

    def _empty_recovery_retry_delay(self, attempts: int) -> int:
        exponent = max(0, min(12, attempts - 1))
        return min(
            MAX_EMPTY_RECOVERY_RETRY_BASE_SECONDS * (2 ** exponent),
            MAX_EMPTY_RECOVERY_RETRY_MAX_SECONDS,
        )

    def _remember_pending_empty_recovery(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        msg_id: str,
        message_type: Optional[str],
        flow_id: str,
    ):
        key = self._pending_empty_recovery_key(chat_id, raw_msg_id)
        now = int(time.time())
        existing = self._pending_empty_recoveries.get(key)
        if existing is None:
            self._pending_empty_recoveries[key] = {
                "chat_id": str(chat_id),
                "raw_msg_id": str(raw_msg_id),
                "msg_id": str(msg_id),
                "message_type": message_type,
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
                "next_attempt_at": now + MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS,
                "last_error": None,
            }
            self._save_pending_empty_recoveries()
            log_event(
                logger,
                logging.INFO,
                "max.inbound.empty_recovery",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="queued",
                reason="durable_history_retry",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                retry_in_seconds=MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS,
            )
            return

        existing["updated_at"] = now
        existing["message_type"] = message_type
        existing["msg_id"] = str(msg_id)
        self._save_pending_empty_recoveries()

    def _forget_pending_empty_recovery(
        self,
        chat_id: str,
        raw_msg_id: str,
        *,
        flow_id: Optional[str] = None,
        reason: str = "recovered",
    ):
        key = self._pending_empty_recovery_key(str(chat_id), str(raw_msg_id))
        if self._pending_empty_recoveries.pop(key, None) is None:
            return
        self._save_pending_empty_recoveries()
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="completed",
            reason=reason,
            max_chat_id=chat_id,
            max_msg_id=raw_msg_id,
        )

    def _start_pending_empty_recovery_worker(self):
        task = self._pending_empty_recovery_worker
        if task is not None and not task.done():
            return
        self._pending_empty_recovery_worker = asyncio.create_task(
            self._run_pending_empty_recoveries()
        )
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery_worker_started",
            stage="startup",
            outcome="started",
            poll_interval_seconds=MAX_EMPTY_RECOVERY_RETRY_POLL_SECONDS,
            pending_count=len(self._pending_empty_recoveries),
        )

    async def _run_pending_empty_recoveries(self):
        while True:
            try:
                now = int(time.time())
                due_jobs = [
                    dict(job)
                    for job in self._pending_empty_recoveries.values()
                    if int(job.get("next_attempt_at") or 0) <= now
                ]
                for job in due_jobs:
                    await self._attempt_pending_empty_recovery(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "max.inbound.empty_recovery_worker_failed",
                    stage="recover",
                    outcome="failed",
                    error=str(e),
                )
            await asyncio.sleep(MAX_EMPTY_RECOVERY_RETRY_POLL_SECONDS)

    async def _attempt_pending_empty_recovery(self, job: dict[str, object]):
        chat_id = str(job.get("chat_id") or "")
        raw_msg_id = str(job.get("raw_msg_id") or "")
        msg_id = str(job.get("msg_id") or raw_msg_id)
        if not chat_id or not raw_msg_id:
            return
        flow_id = build_max_flow_id(chat_id, msg_id)
        key = self._pending_empty_recovery_key(chat_id, raw_msg_id)
        current = self._pending_empty_recoveries.get(key)
        if current is None:
            return

        attempts = int(current.get("attempts") or 0) + 1
        current["attempts"] = attempts
        current["updated_at"] = int(time.time())
        self._save_pending_empty_recoveries()

        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="retry",
            reason="durable_history_retry",
            max_chat_id=chat_id,
            max_msg_id=msg_id,
            attempt=attempts,
        )

        recovered = await self._recover_empty_message_from_recent_history(
            chat_id=chat_id,
            raw_msg_id=raw_msg_id,
            flow_id=flow_id,
        )
        if recovered is not None:
            self._forget_pending_empty_recovery(
                chat_id,
                raw_msg_id,
                flow_id=flow_id,
                reason="durable_history_recovered",
            )
            await self._handle_raw_message(recovered)
            return

        delay = self._empty_recovery_retry_delay(attempts)
        current = self._pending_empty_recoveries.get(key)
        if current is None:
            return
        current["updated_at"] = int(time.time())
        current["next_attempt_at"] = int(time.time()) + delay
        current["last_error"] = "history_message_not_found_or_empty"
        self._save_pending_empty_recoveries()
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="retry_scheduled",
            reason="durable_history_retry",
            max_chat_id=chat_id,
            max_msg_id=msg_id,
            attempt=attempts,
            retry_in_seconds=delay,
        )

    def _schedule_empty_recovery_cache_wait(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        msg_id: str,
        message_type: Optional[str],
        flow_id: str,
    ) -> bool:
        key = (str(chat_id), str(raw_msg_id))
        existing = self._pending_empty_recovery_tasks.get(key)
        if existing is not None and not existing.done():
            return True

        self._remember_pending_empty_recovery(
            chat_id=chat_id,
            raw_msg_id=raw_msg_id,
            msg_id=msg_id,
            message_type=message_type,
            flow_id=flow_id,
        )
        task = asyncio.create_task(
            self._recover_empty_message_from_raw_history_cache_later(
                chat_id=str(chat_id),
                raw_msg_id=str(raw_msg_id),
                msg_id=str(msg_id),
                message_type=message_type,
                flow_id=flow_id,
            )
        )
        self._pending_empty_recovery_tasks[key] = task
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="queued",
            reason="raw_history_cache_wait",
            max_chat_id=chat_id,
            max_msg_id=msg_id,
            wait_seconds=MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS,
        )
        return True

    async def _recover_empty_message_from_raw_history_cache_later(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        msg_id: str,
        message_type: Optional[str],
        flow_id: str,
    ):
        key = (str(chat_id), str(raw_msg_id))
        deadline = time.monotonic() + MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS
        try:
            chat_id_int = int(chat_id)
            raw_msg_id_str = str(raw_msg_id)
        except (TypeError, ValueError):
            self._pending_empty_recovery_tasks.pop(key, None)
            return

        try:
            while time.monotonic() < deadline:
                cached = self._get_cached_raw_history_message(chat_id, raw_msg_id_str)
                if cached is not None:
                    recovered = self._prepare_empty_recovery_candidate(
                        cached,
                        chat_id=chat_id,
                        chat_id_int=chat_id_int,
                        raw_msg_id_str=raw_msg_id_str,
                        flow_id=flow_id,
                        reason="raw_history_cache_delayed_match",
                    )
                    if recovered is not None:
                        self._forget_pending_empty_recovery(
                            chat_id,
                            raw_msg_id,
                            flow_id=flow_id,
                            reason="raw_history_cache_delayed_match",
                        )
                        await self._handle_raw_message(recovered)
                        return

                await asyncio.sleep(MAX_EMPTY_RECOVERY_CACHE_POLL_SECONDS)

            log_event(
                logger,
                logging.INFO,
                "max.inbound.empty_recovery",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="miss",
                reason="raw_history_cache_wait_timeout",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                waited_seconds=MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS,
            )
            log_event(
                logger,
                logging.INFO,
                "max.inbound.skipped",
                flow_id=flow_id,
                direction="inbound",
                stage="normalize",
                outcome="skipped",
                reason="empty_event",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                message_type=message_type,
                has_reaction_info=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "max.inbound.empty_recovery",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="failed",
                reason="raw_history_cache_wait_failed",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                error=str(e),
            )
        finally:
            if self._pending_empty_recovery_tasks.get(key) is asyncio.current_task():
                self._pending_empty_recovery_tasks.pop(key, None)

    async def _handle_raw_receive(self, data: dict):
        """Перехватить channel wrappers до потери вложенного контента в pymax."""
        try:
            from pymax.static.enum import Opcode

            notif_message_opcode = Opcode.NOTIF_MESSAGE.value
            chat_history_opcode = Opcode.CHAT_HISTORY.value
        except Exception:
            notif_message_opcode = 128
            chat_history_opcode = 49

        raw_opcode = data.get("opcode") if isinstance(data, dict) else None
        opcode_value = getattr(raw_opcode, "value", raw_opcode)
        if not isinstance(data, dict):
            return
        if opcode_value != notif_message_opcode:
            if opcode_value == chat_history_opcode:
                self._cache_raw_history_payload(data.get("payload") or {})
            self._log_raw_auxiliary_event(data)
            return

        payload = data.get("payload") or {}
        identity = self._raw_payload_message_identity(payload)
        if identity and self._is_raw_processed_message(*identity):
            return
        if identity:
            self._mark_raw_processed_message(*identity)

        unwrapped = self._build_unwrapped_channel_message(payload)
        if unwrapped is None:
            regular = self._build_raw_regular_message(payload)
            if regular is None:
                message, _outer_chat_id = self._payload_message_dict(payload)
                if message:
                    if self._message_dict_has_content(message):
                        self._log_raw_message_missing_chat_id(payload)
                    else:
                        self._log_raw_empty_message(payload)
                else:
                    self._log_raw_unhandled_message_payload(payload)
                return

            chat_id = str(getattr(regular, "chat_id", "") or "")
            msg_id = str(getattr(regular, "id", "") or "")
            if chat_id and msg_id:
                self._mark_raw_unwrapped_message(chat_id, msg_id)

            await self._handle_raw_message(regular)
            return

        chat_id = str(getattr(unwrapped, "chat_id", "") or "")
        msg_id = str(getattr(unwrapped, "id", "") or "")
        if chat_id and msg_id:
            self._mark_raw_unwrapped_message(chat_id, msg_id)

        await self._handle_raw_message(unwrapped)

    def _install_raw_message_interceptor(self, client):
        if getattr(client, "_maxtg_raw_interceptor_installed", False):
            return client

        original = getattr(client, "_handle_message_notifications", None)
        if original is None:
            log_event(
                logger,
                logging.WARNING,
                "max.raw.interceptor_missing",
                stage="startup",
                outcome="skipped",
                reason="client_has_no_message_notification_handler",
            )
            return client

        async def _handle_message_notifications_with_raw(data: dict):
            await self._handle_raw_receive(data)
            return await original(data)

        _handle_message_notifications_with_raw._maxtg_wrapped = True  # type: ignore[attr-defined]
        client._handle_message_notifications = _handle_message_notifications_with_raw
        client._maxtg_raw_interceptor_installed = True
        handler_count = len(getattr(client, "_on_raw_receive_handlers", []) or [])
        log_event(
            logger,
            logging.INFO,
            "max.raw.interceptor_installed",
            stage="startup",
            outcome="installed",
            raw_handler_count=handler_count,
        )
        return client

    def _get_extra_value(self, extra: dict, *keys: str):
        normalized = {
            str(k).lower().replace("_", ""): v
            for k, v in extra.items()
        }
        for key in keys:
            candidate = key.lower().replace("_", "")
            if candidate in normalized:
                return normalized[candidate]
        return None

    def _coerce_user_ids(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value if v is not None]
        return [str(value)]

    async def _render_user_list(self, user_ids: list[str]) -> Optional[str]:
        if not user_ids:
            return None

        names: list[str] = []
        unresolved = 0
        for uid in user_ids:
            name = await self.resolve_user_name(uid)
            if name:
                names.append(name)
            else:
                unresolved += 1

        if names:
            if unresolved:
                names.append(f"ещё {unresolved}")
            return ", ".join(names)

        if len(user_ids) == 1:
            return "участник"
        return f"{len(user_ids)} участников"

    async def _render_control_attach(self, attach, sender_id: Optional[str],
                                     sender_name: Optional[str]) -> Optional[str]:
        event = str(getattr(attach, "event", "") or "").lower()
        extra = getattr(attach, "extra", None) or {}
        user_ids = self._coerce_user_ids(
            self._get_extra_value(extra, "user_ids", "userIds", "users", "members")
        )
        rendered_users = await self._render_user_list(user_ids)
        title = self._get_extra_value(extra, "title", "theme", "name")
        actor = sender_name

        if event in {"add", "invite", "join", "joined", "joinbylink", "join_by_link", "joinedbylink"}:
            if event in {"joinbylink", "join_by_link", "joinedbylink"}:
                if rendered_users:
                    return f"Присоединились по ссылке: {rendered_users}"
                # Имя присоединившегося может прийти через sender
                name = actor
                if not name and sender_id:
                    name = await self.resolve_user_name(sender_id)
                if name:
                    return f"Присоединился по ссылке: {name}"
                return "Участник присоединился по ссылке"
            if rendered_users:
                return f"Добавлены участники: {rendered_users}"
            return "В чат добавлен участник"

        if event in {"leave", "left", "exit"}:
            if actor:
                return f"{actor} вышел(а) из чата"
            if sender_id:
                resolved_actor = await self.resolve_user_name(sender_id)
                if resolved_actor:
                    return f"{resolved_actor} вышел(а) из чата"
            return "Участник вышел из чата"

        if event in {"remove", "removed", "kick"}:
            if rendered_users:
                return f"Удалены участники: {rendered_users}"
            return "Участник удалён из чата"

        if event in {"new", "create", "created"}:
            if title:
                return f"Создан чат «{title}»"
            if rendered_users:
                return f"Создан новый чат, участники: {rendered_users}"
            return "Создан новый чат"

        if event in {"rename", "title", "theme"}:
            if title:
                return f"Изменено название чата: «{title}»"
            return "Изменено название чата"

        if event in {"description", "about", "profile"}:
            return "Изменён профиль чата"

        if event:
            details: list[str] = []
            if title:
                details.append(f"«{title}»")
            if rendered_users:
                details.append(rendered_users)
            suffix = f": {', '.join(details)}" if details else ""
            return f"Системное событие MAX `{event}`{suffix}"

        return "Системное событие MAX"

    def _render_contact_attach(self, attach) -> str:
        name = getattr(attach, "name", None) or " ".join(
            part for part in [
                getattr(attach, "first_name", None),
                getattr(attach, "last_name", None),
            ] if part
        ).strip()
        return f"Контакт: {name or 'без имени'}"

    def _render_sticker_attach(self, attach) -> str:
        if getattr(attach, "audio", False):
            return "[Аудиостикер]"
        return "[Стикер]"

    def _attachment_log_summary(self, attachments: list["MaxAttachment"]) -> list[dict[str, object]]:
        return [
            {
                "kind": attachment.kind,
                "source_type": attachment.source_type,
                "filename": sanitize_path(attachment.filename or attachment.local_path),
                "duration": attachment.duration,
                "width": attachment.width,
                "height": attachment.height,
            }
            for attachment in attachments
        ]

    def _attachment_failure_log_summary(
        self,
        failures: list["MaxAttachmentFailure"],
    ) -> list[dict[str, object]]:
        return [
            {
                "kind": failure.kind,
                "source_type": failure.source_type,
                "filename": sanitize_path(failure.filename),
                "index": failure.index,
                "reason": failure.reason,
                "retryable": failure.retryable,
                "reference_kind": failure.reference_kind,
            }
            for failure in failures
        ]

    def _attachment_kind_for_type(self, atype: str) -> str:
        if atype == "PHOTO":
            return "photo"
        if atype == "VIDEO":
            return "video"
        if atype == "AUDIO":
            return "audio"
        if atype == "FILE":
            return "document"
        return atype.lower() or "unknown"

    def _build_attachment_failure(
        self,
        *,
        atype: str,
        raw_type: str,
        attach,
        index: int,
        filename: Optional[str],
        media_chat_id: str,
        media_msg_id: str,
        reason: str = "download_failed",
    ) -> MaxAttachmentFailure:
        retryable = False
        reference_kind = None
        reference_id = None
        if atype == "VIDEO":
            video_id = (
                getattr(attach, "video_id", None)
                or getattr(attach, "videoId", None)
                or getattr(attach, "id", None)
            )
            if video_id is not None:
                retryable = True
                reference_kind = "video_id"
                reference_id = str(video_id)
        elif atype == "AUDIO":
            audio_id = getattr(attach, "audio_id", None) or getattr(attach, "audioId", None)
            file_id = getattr(attach, "file_id", None) or getattr(attach, "fileId", None)
            if audio_id is not None:
                retryable = True
                reference_kind = "audio_id"
                reference_id = str(audio_id)
            elif file_id is not None:
                retryable = True
                reference_kind = "file_id"
                reference_id = str(file_id)

        return MaxAttachmentFailure(
            kind=self._attachment_kind_for_type(atype),
            source_type=raw_type,
            filename=filename,
            index=index,
            reason=reason,
            retryable=retryable,
            media_chat_id=str(media_chat_id) if media_chat_id is not None else None,
            media_msg_id=str(media_msg_id) if media_msg_id is not None else None,
            reference_kind=reference_kind,
            reference_id=reference_id,
            duration=getattr(attach, "duration", None),
            width=getattr(attach, "width", None),
            height=getattr(attach, "height", None),
        )

    def _should_skip_empty_event(self, message_type: Optional[str], text: Optional[str],
                                 attachments: list["MaxAttachment"],
                                 rendered_texts: list[str], reaction_info,
                                 attachment_failures: list["MaxAttachmentFailure"] | None = None) -> bool:
        if text or attachments or rendered_texts or attachment_failures:
            return False

        normalized_type = str(message_type or "").upper()
        if reaction_info is not None:
            return True

        return normalized_type in {"", "TEXT", "USER"}

    async def send_message(self, chat_id: str, text: str,
                           reply_to_msg_id: Optional[str] = None,
                           media_path: Optional[str] = None,
                           media_type: Optional[str] = None,
                           flow_id: Optional[str] = None) -> Optional[str]:
        """Отправить сообщение в MAX чат (текст и/или медиа).

        media_type: "photo" | "video" | "audio" | "document"

        Возвращает:
          str  — real max_msg_id
          None — ошибка
        """
        # Ждём подключения до 15 секунд (на случай reconnect)
        self._set_last_outbound_failure(None, attempts=0)
        if not self._started:
            log_event(
                logger,
                logging.ERROR,
                "max.outbound.failed",
                flow_id=flow_id,
                direction="outbound",
                stage="transport",
                outcome="failed",
                reason="not_connected",
                max_chat_id=chat_id,
                media_type=media_type,
            )
            for _ in range(3):
                await asyncio.sleep(5)
                if self._started:
                    break
            else:
                self._set_last_outbound_failure("MAX adapter is not connected", attempts=1)
                return None

        if not self._client:
            self._set_last_outbound_failure("MAX client is not initialized", attempts=1)
            return None

        normalized_text = self._normalize_outbound_text(text)
        max_attempts = 3
        retry_delays = (1, 2)

        for attempt in range(1, max_attempts + 1):
            loop = asyncio.get_running_loop()
            pending = PendingOutboundAck(
                chat_id=str(chat_id),
                text=normalized_text,
                reply_to_msg_id=reply_to_msg_id,
                created_monotonic=time.monotonic(),
                future=loop.create_future(),
            )
            self._pending_outbound_acks.append(pending)
            log_event(
                logger,
                logging.INFO,
                "max.outbound.send",
                flow_id=flow_id,
                direction="outbound",
                stage="transport",
                outcome="started",
                max_chat_id=chat_id,
                media_type=media_type,
                has_text=bool(normalized_text),
                reply_to_max_id=reply_to_msg_id,
                filename=sanitize_path(media_path),
                attempt=attempt,
                max_attempts=max_attempts,
            )

            try:
                from pymax.files import File, Photo, Video

                attachment = None
                if media_path and Path(media_path).exists():
                    if media_type == "photo":
                        attachment = Photo(path=media_path)
                    elif media_type == "video":
                        attachment = Video(path=media_path)
                    else:  # audio, document
                        attachment = File(path=media_path)

                kwargs: dict = {"chat_id": int(chat_id), "text": text}
                if reply_to_msg_id:
                    kwargs["reply_to"] = int(reply_to_msg_id)
                if attachment is not None:
                    kwargs["attachment"] = attachment
                result = await self._client.send_message(**kwargs)
                msg_id = self._extract_result_msg_id(result)
                if msg_id:
                    self._remember_expected_outbound_id(chat_id, msg_id)
                    self._set_last_outbound_failure(None, attempts=attempt)
                    log_event(
                        logger,
                        logging.INFO,
                        "max.outbound.sent",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="sent",
                        max_chat_id=chat_id,
                        max_msg_id=msg_id,
                        media_type=media_type,
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                    return msg_id

                if not normalized_text:
                    error = "MAX send returned no message id"
                    self._set_last_outbound_failure(error, attempts=attempt)
                    log_event(
                        logger,
                        logging.ERROR,
                        "max.outbound.failed",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="failed",
                        reason="max_send_failed",
                        max_chat_id=chat_id,
                        media_type=media_type,
                        error=error,
                        attempts=attempt,
                    )
                    return None

                try:
                    echoed_id = await asyncio.wait_for(asyncio.shield(pending.future), timeout=10)
                    self._set_last_outbound_failure(None, attempts=attempt)
                    log_event(
                        logger,
                        logging.INFO,
                        "max.outbound.sent",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="sent",
                        max_chat_id=chat_id,
                        max_msg_id=str(echoed_id),
                        media_type=media_type,
                        reason="echo_ack",
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                    return str(echoed_id)
                except asyncio.TimeoutError:
                    error = "MAX outbound ack timeout"
                    if attempt < max_attempts:
                        retry_in_seconds = retry_delays[attempt - 1]
                        log_event(
                            logger,
                            logging.WARNING,
                            "max.outbound.retry",
                            flow_id=flow_id,
                            direction="outbound",
                            stage="transport",
                            outcome="retry",
                            reason="ack_timeout",
                            max_chat_id=chat_id,
                            media_type=media_type,
                            error=error,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            retry_in_seconds=retry_in_seconds,
                        )
                        await asyncio.sleep(retry_in_seconds)
                        continue

                    self._set_last_outbound_failure(error, attempts=attempt)
                    log_event(
                        logger,
                        logging.ERROR,
                        "max.outbound.failed",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="failed",
                        reason="ack_timeout",
                        max_chat_id=chat_id,
                        media_type=media_type,
                        error=error,
                        attempts=attempt,
                    )
                    return None
            except Exception as e:
                error = str(e)
                retryable = self._is_retryable_send_error(e)
                if retryable and attempt < max_attempts:
                    retry_in_seconds = retry_delays[attempt - 1]
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.outbound.retry",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="retry",
                        reason="transport_error",
                        max_chat_id=chat_id,
                        media_type=media_type,
                        error=error,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        retry_in_seconds=retry_in_seconds,
                    )
                    await asyncio.sleep(retry_in_seconds)
                    continue

                self._set_last_outbound_failure(error, attempts=attempt)
                log_event(
                    logger,
                    logging.ERROR,
                    "max.outbound.failed",
                    flow_id=flow_id,
                    direction="outbound",
                    stage="transport",
                    outcome="failed",
                    reason="max_send_failed",
                    max_chat_id=chat_id,
                    media_type=media_type,
                    error=error,
                    attempts=attempt,
                    retryable=retryable,
                )
                return None
            finally:
                if pending in self._pending_outbound_acks:
                    self._pending_outbound_acks.remove(pending)
                if not pending.future.done():
                    pending.future.cancel()

        return None

    async def resolve_user_name(self, user_id: str) -> Optional[str]:
        """Получить имя пользователя по ID (для DM чатов без названия).
        Сначала пробует кеш (не требует сокета), затем live-запрос.
        """
        if not self._client:
            return None
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return None

        # 1. Из кеша (синхронно, всегда доступно после sync)
        try:
            cached = self._client.get_cached_user(user_id_int)
            if cached:
                name = self._extract_user_name(cached)
                if name:
                    logger.debug("resolve_user_name (cache) user_id=%s → %r", user_id, name)
                    return name
        except Exception as e:
            logger.debug("get_cached_user failed user_id=%s: %s", user_id, e)

        for source_name, users in (
            ("contacts", getattr(self._client, "contacts", []) or []),
            ("users_cache", (getattr(self._client, "_users", {}) or {}).values()),
        ):
            try:
                for user in users:
                    if str(getattr(user, "id", "") or "") != str(user_id_int):
                        continue
                    name = self._extract_user_name(user)
                    if name:
                        logger.debug(
                            "resolve_user_name (%s) user_id=%s → %r",
                            source_name,
                            user_id,
                            name,
                        )
                        return name
            except Exception as e:
                logger.debug("resolve_user_name %s lookup failed user_id=%s: %s", source_name, user_id, e)

        # 2. Live-запрос (требует активного сокета)
        try:
            users = await asyncio.wait_for(self._client.get_users([user_id_int]), timeout=5)
            if users:
                name = self._extract_user_name(users[0])
                logger.debug("resolve_user_name (live) user_id=%s → %r", user_id, name)
                return name or None
        except asyncio.TimeoutError:
            logger.warning("resolve_user_name timed out user_id=%s", user_id)
        except Exception as e:
            logger.warning("resolve_user_name failed user_id=%s: %s", user_id, e)
        return None

    async def resolve_chat_title(self, chat_id: str) -> Optional[str]:
        """Получить название группового чата по ID.
        Сначала пробует локальный кеш pymax, затем live-запрос к MAX API.
        """
        if not self._client:
            return None

        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return None

        if chat_id_int > 0:
            return None

        try:
            chat_obj = next(
                (chat for chat in getattr(self._client, "chats", []) if getattr(chat, "id", None) == chat_id_int),
                None,
            )
            if chat_obj:
                title = getattr(chat_obj, "title", None) or getattr(chat_obj, "name", None)
                if title:
                    logger.debug("resolve_chat_title (cache) chat_id=%s -> %r", chat_id, title)
                    return title
        except Exception as e:
            logger.debug("resolve_chat_title cache failed chat_id=%s: %s", chat_id, e)

        try:
            chat_obj = await self._client.get_chat(chat_id_int)
            if chat_obj:
                title = getattr(chat_obj, "title", None) or getattr(chat_obj, "name", None)
                if title:
                    logger.debug("resolve_chat_title (live) chat_id=%s -> %r", chat_id, title)
                    return title
        except Exception as e:
            logger.warning("resolve_chat_title failed chat_id=%s: %s", chat_id, e)

        return None

    def get_own_id(self) -> Optional[str]:
        """Вернуть ID нашего MAX аккаунта (для фильтрации собственных сообщений)."""
        return self._own_id

    def find_user_by_name(self, name: str) -> Optional[str]:
        """Найти user_id по отображаемому имени (регистронезависимо).

        Поиск в трёх источниках (от быстрого к более широкому):
          1. client.contacts — контакты, загруженные при sync.
          2. Кеш участников известных DM-диалогов (client.dialogs).
          3. client._users — все пользователи, чьи имена были резолвнуты
             в этой сессии (каждый отправитель любого сообщения в известные чаты).

        Возвращает str(user_id) или None если не найден.
        Если несколько пользователей с одинаковым именем — возвращает первого.
        """
        if not self._client:
            return None
        name_lower = name.strip().lower()

        # 1. Контакты из sync
        for contact in getattr(self._client, "contacts", []):
            contact_name = self._extract_user_name(contact)
            if contact_name and contact_name.strip().lower() == name_lower:
                return str(contact.id)

        # 2. Участники известных DM-диалогов через user cache
        own_id = self._own_id
        for dialog in getattr(self._client, "dialogs", []):
            for pid in (dialog.participants or {}):
                if str(pid) == own_id:
                    continue
                try:
                    user = self._client.get_cached_user(int(pid))
                    if user:
                        user_name = self._extract_user_name(user)
                        if user_name and user_name.strip().lower() == name_lower:
                            return str(pid)
                except Exception:
                    pass

        # 3. Полный кеш пользователей сессии (_users): все отправители всех
        #    сообщений, прошедших через bridge (группы + DM).
        users_cache: dict = getattr(self._client, "_users", {})
        for uid, user in users_cache.items():
            if str(uid) == own_id:
                continue
            try:
                user_name = self._extract_user_name(user)
                if user_name and user_name.strip().lower() == name_lower:
                    return str(uid)
            except Exception:
                pass

        return None

    def get_dm_partner_id(self, chat_id: str) -> Optional[str]:
        """Для DM-чата вернуть user_id СОБЕСЕДНИКА (не нашего аккаунта).

        Использует кеш dialogs из pymax (populated при sync).
        Нужен когда наш аккаунт инициировал чат: в этом случае chat_id может
        совпадать с own_id, и resolve_user_name(chat_id) вернёт наше имя.
        Возвращает None если диалог не найден или собеседник не определён.
        """
        if not self._client or not self._own_id:
            return None
        try:
            chat_id_int = int(chat_id)
            dialog = next(
                (d for d in getattr(self._client, "dialogs", []) if d.id == chat_id_int),
                None,
            )
            if dialog:
                for pid in (dialog.participants or {}):
                    if str(pid) != self._own_id:
                        return str(pid)
        except Exception:
            pass
        return None

    def _attachment_type_name(self, attach) -> str:
        atype = getattr(attach, "type", None)
        if atype is None:
            return ""
        return str(getattr(atype, "value", atype)).upper()

    def _normalize_attachment_type(self, atype: str) -> str:
        if not atype:
            return ""
        upper = str(atype).upper()
        if upper.startswith(("PHOTO", "IMAGE")):
            return "PHOTO"
        if upper.startswith("VIDEO"):
            return "VIDEO"
        if upper.startswith(("AUDIO", "VOICE")):
            return "AUDIO"
        if upper.startswith(("FILE", "DOCUMENT", "DOC")):
            return "FILE"
        return upper

    def _attachment_filename(self, attach) -> Optional[str]:
        name = getattr(attach, "filename", None) or getattr(attach, "name", None)
        return self._fix_filename_encoding(name) if name else None

    def _safe_attachment_field_names(self, attach) -> list[str]:
        try:
            names = vars(attach).keys()
        except TypeError:
            names = (
                name
                for name in dir(attach)
                if not name.startswith("_") and not callable(getattr(attach, name, None))
            )
        return sorted(
            name
            for name in names
            if self._is_safe_field_name(name)
        )

    @staticmethod
    def _fix_filename_encoding(name: str) -> str:
        """Fix cp1251-as-latin-1 mojibake in filenames from MAX.

        MAX CDN sometimes returns filenames with cp1251 bytes decoded as latin-1,
        producing garbled text like "Âàëüñ" instead of "Вальс".
        Heuristic: if the string fits in latin-1 and decodes cleanly as cp1251, use it.
        Pure ASCII and already-correct UTF-8 strings pass through unchanged.
        """
        try:
            fixed = name.encode("latin-1").decode("cp1251")
            return fixed if fixed != name else name
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name

    def _build_filename(self, prefix: str, filename_hint: Optional[str],
                        url: Optional[str], content_type: Optional[str],
                        default_extension: str = "") -> str:
        base_name = Path(filename_hint).name if filename_hint else ""
        stem = Path(base_name).stem if base_name else prefix
        suffix = Path(base_name).suffix

        if not suffix and url:
            suffix = Path(urlparse(url).path).suffix

        if not suffix and content_type:
            guessed = mimetypes.guess_extension(content_type)
            if guessed == ".jpe":
                guessed = ".jpg"
            suffix = guessed or ""

        if not suffix and default_extension:
            suffix = default_extension if default_extension.startswith(".") else f".{default_extension}"

        return f"{stem}{suffix}" if suffix else stem

    def _extract_video_url(self, value, *, key_hint: Optional[str] = None) -> Optional[str]:
        """Найти реальный URL видео в сыром payload VIDEO_PLAY.

        pymax разбирает VIDEO_PLAY довольно хрупко: берёт первое поле payload,
        которое не EXTERNAL/cache. На практике сервер может вернуть вложенную
        структуру или сначала preview/thumbnail. Здесь ищем лучший URL сами.
        """
        candidates: list[tuple[int, str]] = []

        def score_url(url: str, key: Optional[str]) -> int:
            score = 0
            lowered_url = url.lower()
            lowered_key = (key or "").lower()

            if lowered_key in {"url", "src", "source"} or lowered_key.isdigit():
                score += 4
            if "video" in lowered_key or "stream" in lowered_key:
                score += 3
            if "mp4" in lowered_key or "m3u8" in lowered_key or "hls" in lowered_key:
                score += 6
            if any(resolution in lowered_key for resolution in ("144", "240", "360", "480", "720", "1080", "1440", "2160")):
                score += 2
            if any(ext in lowered_url for ext in (".mp4", ".mov", ".m4v", ".webm", ".m3u8")):
                score += 5
            if lowered_key == "external":
                score -= 12
            if any(marker in lowered_key for marker in ("thumbnail", "thumb", "preview")):
                score -= 5
            if "m.ok.ru/video/" in lowered_url or "ok.ru/video/" in lowered_url:
                score -= 8
            if any(ext in lowered_url for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                score -= 6

            return score

        def walk(node, key: Optional[str] = None):
            if isinstance(node, str):
                if node.startswith(("http://", "https://")):
                    candidates.append((score_url(node, key), node))
                return

            if isinstance(node, dict):
                for nested_key, nested_value in node.items():
                    walk(nested_value, str(nested_key))
                return

            if isinstance(node, (list, tuple, set)):
                for nested_value in node:
                    walk(nested_value, key)
                return

            url_attr = getattr(node, "url", None)
            if url_attr is not None and url_attr is not node:
                walk(url_attr, "url")

            if hasattr(node, "__dict__"):
                walk(vars(node), key)

        walk(value, key_hint)
        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _download_client_profile_for_url(self, url: str) -> tuple[dict[str, str], Optional[str], str]:
        src_ag = (
            parse_qs(urlparse(url).query).get("srcAg", [None])[0]
            if url else None
        )
        normalized = str(src_ag or "").upper()
        if "CHROME" in normalized and "ANDROID" in normalized:
            user_agent = MAX_CDN_ANDROID_CHROME_USER_AGENT
            ua_family = "chrome_android"
        elif "CHROME" in normalized and ("IPHONE" in normalized or "IOS" in normalized):
            user_agent = MAX_CDN_IOS_CHROME_USER_AGENT
            ua_family = "chrome_ios"
        elif "CHROME" in normalized:
            user_agent = MAX_CDN_CHROME_USER_AGENT
            ua_family = "chrome_desktop"
        else:
            user_agent = MAX_CDN_USER_AGENT
            ua_family = "safari_mobile"
        return {"User-Agent": user_agent}, src_ag, ua_family

    def _download_headers_for_url(self, url: str) -> dict[str, str]:
        headers, _src_ag, _ua_family = self._download_client_profile_for_url(url)
        return headers

    def _detect_magic_type(self, content: bytes) -> str:
        if not content:
            return "unknown"

        head = content[:64]

        if head.startswith(b"\xff\xd8\xff"):
            return "image"
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image"
        if head.startswith((b"GIF87a", b"GIF89a")):
            return "image"
        if head.startswith(b"RIFF") and b"WEBP" in content[8:16]:
            return "image"

        if len(content) > 12 and content[4:8] == b"ftyp":
            return "video"
        if head.startswith(b"\x1a\x45\xdf\xa3"):
            return "video"  # webm/mkv

        if head.startswith(b"OggS"):
            return "audio"
        if head.startswith(b"ID3"):
            return "audio"

        if head.startswith(b"%PDF"):
            return "document"
        if head.startswith(b"PK\x03\x04"):
            return "document"

        lowered = content[:256].lstrip().lower()
        if lowered.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
            return "html"
        return "unknown"

    def _classify_downloaded_content(self, content_type: Optional[str], content: bytes) -> str:
        magic_type = self._detect_magic_type(content)
        if magic_type != "unknown":
            return magic_type

        normalized = str(content_type or "").lower()
        if normalized.startswith("image/"):
            return "image"
        if normalized.startswith("video/"):
            return "video"
        if normalized.startswith("audio/"):
            return "audio"
        if normalized.startswith("text/html"):
            return "html"
        if normalized.startswith("text/"):
            return "text"
        if normalized.startswith("application/"):
            return "document"
        return "unknown"

    def _is_download_valid(self, expected_kind: Optional[str], detected_kind: str) -> bool:
        if detected_kind == "html":
            return False

        expected = str(expected_kind or "").lower()
        if not expected:
            if detected_kind == "text":
                return False
            return True

        if detected_kind == "text":
            return expected == "document"

        expected_map = {
            "photo": {"image"},
            "video": {"video"},
            "audio": {"audio"},
            "document": {"document", "image", "video", "audio", "unknown"},
        }
        allowed = expected_map.get(expected)
        if not allowed:
            return True
        if detected_kind in allowed:
            return True
        if detected_kind == "unknown":
            return True
        return False

    def _is_retryable_download_error(self, error: Exception) -> bool:
        if isinstance(error, ClientResponseError):
            return error.status not in {401, 403, 404, 410}
        return True

    def _download_error_for_log(self, error: Exception) -> str:
        if isinstance(error, ClientResponseError):
            message = str(error.message or "").strip()
            return f"HTTP {error.status}" + (f" {message}" if message else "")
        return str(error)

    def _download_error_status(self, error: Exception) -> Optional[int]:
        if isinstance(error, ClientResponseError):
            return error.status
        return None

    async def _write_download_response(self, response, part_path: Path, mode: str) -> int:
        written = 0
        with part_path.open(mode) as fh:
            stream = getattr(getattr(response, "content", None), "iter_chunked", None)
            if callable(stream):
                async for chunk in stream(MAX_DOWNLOAD_CHUNK_SIZE):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
            else:
                content = await response.read()
                fh.write(content)
                written += len(content)
        return written

    def _download_retry_delay(self, attempt: int) -> int:
        return min(2 ** (attempt - 1), 8)

    async def _download_from_url(self, url: str, prefix: str,
                                 filename_hint: Optional[str] = None,
                                 default_extension: str = "",
                                 expected_kind: Optional[str] = None,
                                 flow_id: Optional[str] = None,
                                 download_source: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Скачать файл по URL, вернуть (local_path, filename)."""
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        filename = self._build_filename(prefix, filename_hint, url, None, default_extension)
        local_path = self._tmp_dir / filename
        part_path = self._tmp_dir / f"{filename}.part"
        last_error: Exception | None = None
        content_type: Optional[str] = None

        for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
            resume_from = part_path.stat().st_size if part_path.exists() else 0
            headers, src_ag, ua_family = self._download_client_profile_for_url(url)
            if resume_from:
                headers = {**headers, "Range": f"bytes={resume_from}-"}

            try:
                async with ClientSession(headers=headers) as session:
                    async with session.get(url) as response:
                        http_status = getattr(response, "status", None)
                        if resume_from and getattr(response, "status", None) == 200:
                            part_path.unlink(missing_ok=True)
                            resume_from = 0
                            log_event(
                                logger,
                                logging.INFO,
                                "max.attachment.download_resume",
                                flow_id=flow_id,
                                direction="inbound",
                                stage="download",
                                outcome="unsupported",
                                source=sanitize_url(url),
                                download_source=download_source,
                                src_ag=src_ag,
                                ua_family=ua_family,
                                http_status=http_status,
                                attempt=attempt,
                            )

                        response.raise_for_status()
                        content_type = response.headers.get("Content-Type", "").split(";")[0].strip() or None
                        mode = "ab" if resume_from and getattr(response, "status", None) == 206 else "wb"
                        bytes_written = await self._write_download_response(response, part_path, mode)

                if bytes_written <= 0 and not part_path.exists():
                    raise RuntimeError("download returned no content")

                content = part_path.read_bytes()
                detected_kind = self._classify_downloaded_content(content_type, content)
                if not self._is_download_valid(expected_kind, detected_kind):
                    part_path.unlink(missing_ok=True)
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.attachment.download",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="download",
                        outcome="rejected",
                        reason="download_rejected",
                        expected_kind=expected_kind,
                        detected_kind=detected_kind,
                        content_type=content_type,
                        source=sanitize_url(url),
                        download_source=download_source,
                        src_ag=src_ag,
                        ua_family=ua_family,
                        http_status=http_status,
                        attempts=attempt,
                    )
                    return None, None

                part_path.replace(local_path)
                log_event(
                    logger,
                    logging.INFO,
                    "max.attachment.download",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="downloaded",
                    expected_kind=expected_kind,
                    detected_kind=detected_kind,
                    content_type=content_type,
                    source=sanitize_url(url),
                    download_source=download_source,
                    src_ag=src_ag,
                    ua_family=ua_family,
                    http_status=http_status,
                    filename=sanitize_path(filename),
                    size_bytes=local_path.stat().st_size,
                    attempts=attempt,
                    resumed=attempt > 1 or bool(resume_from),
                )
                return str(local_path), filename
            except Exception as e:
                last_error = e
                retryable = self._is_retryable_download_error(e)
                if retryable and attempt < MAX_DOWNLOAD_ATTEMPTS:
                    retry_in_seconds = self._download_retry_delay(attempt)
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.attachment.download_retry",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="download",
                        outcome="retry",
                        reason="download_failed",
                        expected_kind=expected_kind,
                        source=sanitize_url(url),
                        download_source=download_source,
                        src_ag=src_ag,
                        ua_family=ua_family,
                        http_status=self._download_error_status(e),
                        error=self._download_error_for_log(e),
                        attempt=attempt,
                        max_attempts=MAX_DOWNLOAD_ATTEMPTS,
                        resume_from_bytes=part_path.stat().st_size if part_path.exists() else 0,
                        retry_in_seconds=retry_in_seconds,
                    )
                    await asyncio.sleep(retry_in_seconds)
                    continue

                log_event(
                    logger,
                    logging.WARNING,
                    "max.attachment.download",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="failed",
                    reason="download_failed",
                    expected_kind=expected_kind,
                    source=sanitize_url(url),
                    download_source=download_source,
                    src_ag=src_ag,
                    ua_family=ua_family,
                    http_status=self._download_error_status(e),
                    error=self._download_error_for_log(e),
                    attempts=attempt,
                    max_attempts=MAX_DOWNLOAD_ATTEMPTS,
                    retryable=retryable,
                    resume_from_bytes=part_path.stat().st_size if part_path.exists() else 0,
                )
                break

        if last_error is not None:
            part_path.unlink(missing_ok=True)
        return None, None

    async def _download_file_by_id(self, chat_id: str, msg_id: str, file_id: int,
                                   prefix: str, filename_hint: Optional[str] = None,
                                   default_extension: str = "",
                                   expected_kind: Optional[str] = None,
                                   flow_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Скачать файл через pymax FILE_DOWNLOAD."""
        if not self._client:
            return None, None
        try:
            file_obj = await self._client.get_file_by_id(
                chat_id=int(chat_id),
                message_id=int(msg_id),
                file_id=int(file_id),
            )
            url = getattr(file_obj, "url", None)
            if not url:
                return None, None
            return await self._download_from_url(
                url,
                prefix,
                filename_hint,
                default_extension,
                expected_kind=expected_kind,
                flow_id=flow_id,
                download_source="file_download",
            )
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.attachment.download",
                flow_id=flow_id,
                direction="inbound",
                stage="download",
                outcome="failed",
                reason="file_download_failed",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                error=str(e),
            )
        return None, None

    async def _download_video_by_id(self, chat_id: str, msg_id: str, video_id: int,
                                    prefix: str, filename_hint: Optional[str] = None,
                                    flow_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Скачать видео через pymax VIDEO_PLAY."""
        if not self._client:
            return None, None
        try:
            from pymax.payloads import GetVideoPayload
            from pymax.static.enum import Opcode

            payload = GetVideoPayload(
                chat_id=int(chat_id),
                message_id=int(msg_id),
                video_id=int(video_id),
            ).model_dump(by_alias=True)
            data = await self._client._send_and_wait(opcode=Opcode.VIDEO_PLAY, payload=payload)
            raw_payload = data.get("payload") if isinstance(data, dict) else None
            url = self._extract_video_url(raw_payload)
            if not url:
                log_event(
                    logger,
                    logging.WARNING,
                    "max.attachment.download",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="failed",
                    reason="video_url_missing",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                )
                return None, None
            return await self._download_from_url(
                url,
                prefix,
                filename_hint,
                ".mp4",
                expected_kind="video",
                flow_id=flow_id,
                download_source="video_play",
            )
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.attachment.download",
                flow_id=flow_id,
                direction="inbound",
                stage="download",
                outcome="failed",
                reason="video_download_failed",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                error=str(e),
            )
        return None, None

    async def download_video_reference(
        self,
        *,
        chat_id: str,
        msg_id: str,
        video_id: str,
        attachment_index: int = 0,
        filename_hint: Optional[str] = None,
        duration: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        source_type: Optional[str] = "VIDEO",
        flow_id: Optional[str] = None,
    ) -> Optional[MaxAttachment]:
        """Скачать видео по стабильной ссылке MAX без хранения signed URL."""
        idx = f"_{attachment_index}" if attachment_index > 0 else ""
        try:
            video_id_int = int(video_id)
        except (TypeError, ValueError):
            return None

        local_path, filename = await self._download_video_by_id(
            chat_id,
            msg_id,
            video_id_int,
            f"video_retry_{chat_id}_{msg_id}{idx}",
            filename_hint,
            flow_id=flow_id,
        )
        if not local_path:
            return None
        return MaxAttachment(
            kind="video",
            local_path=local_path,
            filename=filename,
            duration=duration,
            width=width,
            height=height,
            source_type=source_type,
        )

    async def download_audio_reference(
        self,
        *,
        chat_id: str,
        msg_id: str,
        reference_id: str,
        reference_kind: str = "audio_id",
        attachment_index: int = 0,
        filename_hint: Optional[str] = None,
        duration: Optional[int] = None,
        source_type: Optional[str] = "AUDIO",
        flow_id: Optional[str] = None,
    ) -> Optional[MaxAttachment]:
        """Retry MAX audio without persisting signed URLs."""
        idx = f"_{attachment_index}" if attachment_index > 0 else ""
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return None

        raw_payload = await self._fetch_raw_history_payload(
            chat_id_int=chat_id_int,
            from_time=int(time.time() * 1000) + 60_000,
            forward=0,
            backward=30,
            flow_id=flow_id,
        )
        if raw_payload is not None:
            raw_message = self._find_raw_history_message_dict(raw_payload, str(msg_id))
            if raw_message is not None:
                normalized = self._normalize_message_dict(raw_message)
                raw_attaches = self._payload_value(normalized, "attaches", "attachments") or []
                attach_list = raw_attaches if isinstance(raw_attaches, list) else [raw_attaches]
                for attach in attach_list:
                    if not isinstance(attach, dict):
                        continue
                    attach_obj = SimpleNamespace(**self._normalize_message_dict(attach))
                    atype = self._normalize_attachment_type(
                        self._attachment_type_name(attach_obj)
                    )
                    if atype != "AUDIO":
                        continue
                    attach_refs = {
                        str(value)
                        for value in (
                            getattr(attach_obj, "audio_id", None),
                            getattr(attach_obj, "audioId", None),
                            getattr(attach_obj, "file_id", None),
                            getattr(attach_obj, "fileId", None),
                            getattr(attach_obj, "id", None),
                        )
                        if value is not None
                    }
                    if str(reference_id) not in attach_refs and len(attach_list) > 1:
                        continue
                    attachment = await self._download_attachment(
                        chat_id,
                        msg_id,
                        attach_obj,
                        index=attachment_index,
                        flow_id=flow_id,
                    )
                    if attachment:
                        attachment.duration = attachment.duration or duration
                        attachment.source_type = source_type or attachment.source_type
                        return attachment

        try:
            stable_id = int(reference_id)
        except (TypeError, ValueError):
            return None
        local_path, filename = await self._download_file_by_id(
            chat_id,
            msg_id,
            stable_id,
            f"audio_retry_{chat_id}_{msg_id}{idx}",
            filename_hint,
            ".ogg",
            expected_kind="audio",
            flow_id=flow_id,
        )
        if not local_path:
            return None
        return MaxAttachment(
            kind="audio",
            local_path=local_path,
            filename=filename,
            duration=duration,
            width=None,
            height=None,
            source_type=source_type,
        )

    async def _download_attachment(self, chat_id: str, msg_id: str,
                                   attach, index: int = 0,
                                   flow_id: Optional[str] = None) -> Optional[MaxAttachment]:
        """Скачать одно вложение и нормализовать в MaxAttachment."""
        raw_type = self._attachment_type_name(attach)
        atype = self._normalize_attachment_type(raw_type)
        filename_hint = self._attachment_filename(attach)
        idx = f"_{index}" if index > 0 else ""

        if "PHOTO" in atype or "IMAGE" in atype:
            url = getattr(attach, "base_url", None) or getattr(attach, "baseRawUrl", None) or getattr(attach, "url", None)
            if url:
                local_path, filename = await self._download_from_url(
                    url, f"photo_{chat_id}_{msg_id}{idx}", filename_hint, ".jpg",
                    expected_kind="photo", flow_id=flow_id, download_source="direct_url",
                )
            else:
                file_id = getattr(attach, "file_id", None) or getattr(attach, "id", None)
                if not file_id:
                    return None
                local_path, filename = await self._download_file_by_id(
                    chat_id, msg_id, file_id, f"photo_{chat_id}_{msg_id}{idx}",
                    filename_hint, ".jpg", expected_kind="photo", flow_id=flow_id,
                )
            if local_path:
                return MaxAttachment(
                    kind="photo",
                    local_path=local_path,
                    filename=filename,
                    duration=None,
                    width=getattr(attach, "width", None),
                    height=getattr(attach, "height", None),
                    source_type=raw_type,
                )
            return None

        if "VIDEO" in atype:
            video_id = getattr(attach, "video_id", None) or getattr(attach, "id", None)
            url = getattr(attach, "url", None)
            local_path = None
            filename = None
            if url:
                local_path, filename = await self._download_from_url(
                    url, f"video_{chat_id}_{msg_id}{idx}", filename_hint, ".mp4",
                    expected_kind="video", flow_id=flow_id, download_source="direct_url",
                )
                if not local_path and video_id:
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.attachment.video_fallback",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="download",
                        outcome="retry",
                        reason="direct_url_failed",
                        max_chat_id=chat_id,
                        max_msg_id=msg_id,
                        source=sanitize_url(url),
                        attachment_index=index,
                    )
            if not local_path and video_id:
                local_path, filename = await self._download_video_by_id(
                    chat_id, msg_id, video_id, f"video_{chat_id}_{msg_id}{idx}", filename_hint,
                    flow_id=flow_id,
                )
            if not local_path:
                return None
            if local_path:
                return MaxAttachment(
                    kind="video",
                    local_path=local_path,
                    filename=filename,
                    duration=getattr(attach, "duration", None),
                    width=getattr(attach, "width", None),
                    height=getattr(attach, "height", None),
                    source_type=raw_type,
                )
            return None

        if "AUDIO" in atype or "VOICE" in atype:
            url = getattr(attach, "url", None)
            audio_id = getattr(attach, "audio_id", None) or getattr(attach, "audioId", None)
            file_id = (
                getattr(attach, "file_id", None)
                or getattr(attach, "id", None)
                or audio_id
            )
            local_path = None
            filename = None
            if url:
                local_path, filename = await self._download_from_url(
                    url, f"audio_{chat_id}_{msg_id}{idx}", filename_hint, ".ogg",
                    expected_kind="audio", flow_id=flow_id, download_source="direct_url",
                )
                if not local_path and file_id:
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.attachment.audio_fallback",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="download",
                        outcome="retry",
                        reason="direct_url_failed",
                        max_chat_id=chat_id,
                        max_msg_id=msg_id,
                        attachment_index=index,
                    )
            if not local_path and file_id:
                local_path, filename = await self._download_file_by_id(
                    chat_id, msg_id, file_id, f"audio_{chat_id}_{msg_id}{idx}",
                    filename_hint, ".ogg", expected_kind="audio", flow_id=flow_id,
                )
            if not local_path and not file_id:
                log_event(
                    logger,
                    logging.WARNING,
                    "max.attachment.voice_reference_missing",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="failed",
                    reason="voice_reference_missing",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    source_type=raw_type,
                    attachment_class=attach.__class__.__name__,
                    attachment_fields=self._safe_attachment_field_names(attach),
                    attachment_index=index,
                )
                return None
            if local_path:
                return MaxAttachment(
                    kind="audio",
                    local_path=local_path,
                    filename=filename,
                    duration=getattr(attach, "duration", None),
                    width=None,
                    height=None,
                    source_type=raw_type,
                )
            return None

        if "FILE" in atype or "DOCUMENT" in atype or "DOC" in atype:
            file_id = getattr(attach, "file_id", None) or getattr(attach, "id", None)
            if not file_id:
                return None
            local_path, filename = await self._download_file_by_id(
                chat_id, msg_id, file_id, f"doc_{chat_id}_{msg_id}{idx}",
                filename_hint, expected_kind="document", flow_id=flow_id,
            )
            if local_path:
                return MaxAttachment(
                    kind="document",
                    local_path=local_path,
                    filename=filename,
                    duration=None,
                    width=None,
                    height=None,
                    source_type=raw_type,
                )
            return None

        return None

    def _extract_user_name(self, user_obj) -> Optional[str]:
        """Извлечь имя из pymax User/Contact/Names объекта."""
        if user_obj is None:
            return None
        # User/Contact имеют .names: list[Names], где Names.name, first_name, last_name
        names_list = getattr(user_obj, "names", None)
        if names_list:
            n = names_list[0]
            first = getattr(n, "first_name", None) or getattr(n, "name", None) or ""
            last  = getattr(n, "last_name", None) or ""
            return f"{first} {last}".strip() or None
        # Fallback: прямые атрибуты (для других объектов)
        first = getattr(user_obj, "first_name", None) or getattr(user_obj, "name", None) or ""
        last  = getattr(user_obj, "last_name", None) or ""
        return f"{first} {last}".strip() or None

    async def _handle_raw_message(self, message):
        """Конвертируем raw MAX Message → MaxMessage и вызываем handlers.

        pymax Message fields:
          .id         — int message id
          .chat_id    — int (положительный = DM, отрицательный = группа)
          .sender     — int user_id отправителя (не объект!)
          .text       — str
          .attaches   — list вложений
        """
        try:
            raw_msg_id = str(getattr(message, "id", None) or "")
            chat_id = str(getattr(message, "chat_id", "") or "")
            if (
                raw_msg_id
                and chat_id
                and not getattr(message, "_from_raw_unwrapped", False)
                and self._consume_raw_unwrapped_message(chat_id, raw_msg_id)
            ):
                log_event(
                    logger,
                    logging.DEBUG,
                    "max.inbound.skipped",
                    direction="inbound",
                    stage="received",
                    outcome="skipped",
                    reason="raw_unwrapped_duplicate",
                    max_chat_id=chat_id,
                    max_msg_id=raw_msg_id,
                )
                return

            forwarded = self._extract_forwarded_payload(message)
            content_message = forwarded.message if forwarded else message

            text = (
                getattr(content_message, "text", None)
                or getattr(message, "text", None)
                or None
            )
            if text == "":
                text = None
            message_type = str(
                getattr(content_message, "type", None)
                or getattr(message, "type", None)
                or ""
            ) or None
            status = str(getattr(message, "status", None) or "").upper() or None
            reaction_info = (
                getattr(message, "reactionInfo", None)
                or getattr(message, "reaction_info", None)
                or getattr(content_message, "reactionInfo", None)
                or getattr(content_message, "reaction_info", None)
            )
            msg_id = f"{raw_msg_id}:{status}" if raw_msg_id and status else raw_msg_id
            media_chat_id = (
                forwarded.chat_id
                if forwarded
                else getattr(message, "_forward_source_chat_id", None)
            ) or chat_id
            media_msg_id = (
                forwarded.msg_id
                if forwarded
                else getattr(message, "_forward_source_msg_id", None)
            ) or raw_msg_id

            # Отправитель: message.sender — это int
            sender_int = getattr(message, "sender", None)
            sender_id  = str(sender_int) if sender_int is not None else None
            reply_to_msg_id = self._extract_reply_to_msg_id(message)
            flow_id = build_max_flow_id(chat_id, msg_id or raw_msg_id)

            if not raw_msg_id or not chat_id:
                log_event(
                    logger,
                    logging.DEBUG,
                    "max.inbound.skipped",
                    stage="received",
                    outcome="skipped",
                    reason="missing_identifiers",
                )
                return

            if is_probable_client_cid(chat_id):
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.skipped",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="received",
                    outcome="skipped",
                    reason="probable_client_cid_chat_id",
                    max_msg_id=msg_id,
                    message_type=message_type,
                    message_fields=self._safe_attachment_field_names(message),
                    content_fields=self._safe_attachment_field_names(content_message),
                    **self._safe_message_structure_summary(content_message),
                )
                return

            is_own = bool(self._own_id and sender_id == self._own_id)
            if is_own:
                if self._consume_expected_outbound_id(chat_id, raw_msg_id):
                    log_event(
                        logger,
                        logging.DEBUG,
                        "max.inbound.skipped",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="received",
                        outcome="skipped",
                        reason="expected_echo",
                        max_chat_id=chat_id,
                        max_msg_id=raw_msg_id,
                    )
                    return
                pending = self._claim_pending_outbound_ack(chat_id, text, reply_to_msg_id)
                if pending:
                    if not pending.future.done():
                        pending.future.set_result(raw_msg_id)
                    log_event(
                        logger,
                        logging.DEBUG,
                        "max.inbound.skipped",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="received",
                        outcome="skipped",
                        reason="acknowledged_echo",
                        max_chat_id=chat_id,
                        max_msg_id=raw_msg_id,
                    )
                    return

            # DM: chat_id > 0 (личная переписка), группа/канал: chat_id < 0
            try:
                chat_id_int = int(chat_id)
                is_dm = chat_id_int > 0
            except (ValueError, TypeError):
                is_dm = not chat_id.startswith("-")

            # Название чата: для групп ищем в кеше client.chats
            chat_title = None
            if not is_dm and self._client:
                try:
                    chat_obj = next(
                        (c for c in self._client.chats if c.id == chat_id_int), None
                    )
                    if chat_obj:
                        chat_title = getattr(chat_obj, "title", None)
                except Exception:
                    pass

            attaches = getattr(content_message, "attaches", None) or []
            attach_list = attaches if isinstance(attaches, list) else [attaches]
            raw_attachment_types = [
                self._attachment_type_name(attach)
                for attach in attach_list
                if attach is not None
            ]
            attachment_types = [
                self._normalize_attachment_type(atype)
                for atype in raw_attachment_types
                if atype
            ]

            log_event(
                logger,
                logging.INFO,
                "max.inbound.received",
                flow_id=flow_id,
                direction="inbound",
                stage="received",
                outcome="accepted",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                is_dm=is_dm,
                is_own=is_own,
                message_type=message_type,
                status=status,
                attachment_types=attachment_types,
                has_text=bool(text),
            )

            has_raw_attachments = any(attach is not None for attach in attach_list)
            recoverable_empty_type = str(message_type or "").upper() in {"", "TEXT", "USER"}
            if (
                not text
                and not has_raw_attachments
                and not reaction_info
                and not status
                and recoverable_empty_type
                and not getattr(message, "_from_empty_recovery", False)
            ):
                self._log_typed_empty_message(
                    flow_id=flow_id,
                    message=message,
                    content_message=content_message,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    message_type=message_type,
                    reaction_info=reaction_info,
                )
                recovered = await self._recover_empty_message_from_recent_history(
                    chat_id=chat_id,
                    raw_msg_id=raw_msg_id,
                    flow_id=flow_id,
                )
                if recovered is not None:
                    await self._handle_raw_message(recovered)
                    return
                if self._schedule_empty_recovery_cache_wait(
                    chat_id=chat_id,
                    raw_msg_id=raw_msg_id,
                    msg_id=msg_id,
                    message_type=message_type,
                    flow_id=flow_id,
                ):
                    return
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.skipped",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="normalize",
                    outcome="skipped",
                    reason="empty_event",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    message_type=message_type,
                    has_reaction_info=bool(reaction_info),
                )
                return

            if text or has_raw_attachments:
                self._forget_pending_empty_recovery(
                    chat_id,
                    raw_msg_id,
                    flow_id=flow_id,
                    reason="content_arrived",
                )

            # Имя отправителя: кеш + live fallback (важно для групповых чатов).
            # Для пустых событий recovery уже выполнен выше, чтобы не тратить
            # активный MAX socket на CONTACT_INFO перед CHAT_HISTORY.
            sender_name = None
            if sender_id:
                sender_name = await self.resolve_user_name(sender_id)

            # own_id сохраняем в msg для фильтрации в BridgeCore
            # (не фильтруем здесь — bridge решает сам)

            # Вложения (в pymax Message это .attaches)
            attachments: list[MaxAttachment] = []
            attachment_failures: list[MaxAttachmentFailure] = []
            rendered_texts: list[str] = []
            media_index = 0
            for attach in attach_list:
                if attach is None:
                    continue
                raw_type = self._attachment_type_name(attach)
                atype = self._normalize_attachment_type(raw_type)
                if atype in {"PHOTO", "VIDEO", "AUDIO", "FILE"}:
                    filename_hint = self._attachment_filename(attach)
                    attachment = await self._download_attachment(
                        media_chat_id,
                        media_msg_id,
                        attach,
                        index=media_index,
                        flow_id=flow_id,
                    )
                    media_index += 1
                    if attachment:
                        attachments.append(attachment)
                    else:
                        attachment_failures.append(
                            self._build_attachment_failure(
                                atype=atype,
                                raw_type=raw_type,
                                attach=attach,
                                index=media_index - 1,
                                filename=filename_hint,
                                media_chat_id=media_chat_id,
                                media_msg_id=media_msg_id,
                            )
                        )
                    continue

                if atype == "CONTROL":
                    rendered = await self._render_control_attach(attach, sender_id, sender_name)
                elif atype == "CONTACT":
                    rendered = self._render_contact_attach(attach)
                elif atype == "STICKER":
                    rendered = self._render_sticker_attach(attach)
                else:
                    rendered = f"[Вложение MAX: {raw_type.lower()}]" if raw_type else None

                if rendered:
                    rendered_texts.append(rendered)

            if status == "EDITED":
                rendered_texts.insert(0, "[Сообщение отредактировано]")
            elif status == "REMOVED":
                rendered_texts = ["[Сообщение удалено]"]

            if self._should_skip_empty_event(
                message_type,
                text,
                attachments,
                rendered_texts,
                reaction_info,
                attachment_failures,
            ):
                self._log_typed_empty_message(
                    flow_id=flow_id,
                    message=message,
                    content_message=content_message,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    message_type=message_type,
                    reaction_info=reaction_info,
                )
                if (
                    not reaction_info
                    and not getattr(message, "_from_empty_recovery", False)
                ):
                    recovered = await self._recover_empty_message_from_recent_history(
                        chat_id=chat_id,
                        raw_msg_id=raw_msg_id,
                        flow_id=flow_id,
                    )
                    if recovered is not None:
                        await self._handle_raw_message(recovered)
                        return
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.skipped",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="normalize",
                    outcome="skipped",
                    reason="empty_event",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    message_type=message_type,
                    has_reaction_info=bool(reaction_info),
                )
                return

            if not text and not attachments and not rendered_texts and message_type:
                if message_type.upper() not in {"TEXT", "USER"}:
                    rendered_texts.append(
                        self._render_unknown_message_details(
                            message=message,
                            content_message=content_message,
                            message_type=message_type,
                            status=status,
                            raw_attachment_types=raw_attachment_types,
                            forwarded=forwarded,
                        )
                    )

            log_event(
                logger,
                logging.INFO,
                "max.inbound.normalized",
                flow_id=flow_id,
                direction="inbound",
                stage="normalize",
                outcome="ready",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                attachments=self._attachment_log_summary(attachments),
                attachment_failures=self._attachment_failure_log_summary(attachment_failures),
                failed_attachment_count=len(attachment_failures),
                attachment_types=attachment_types,
                rendered_count=len(rendered_texts),
                has_text=bool(text),
            )
            if text:
                log_event(
                    logger,
                    logging.DEBUG,
                    "max.inbound.preview",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="normalize",
                    outcome="ready",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    preview=text,
                )

            mx_msg = MaxMessage(
                msg_id=msg_id,
                chat_id=chat_id,
                chat_title=chat_title,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                attachments=attachments,
                attachment_types=attachment_types,
                rendered_texts=rendered_texts,
                message_type=message_type,
                status=status,
                is_dm=is_dm,
                is_own=is_own,
                raw=message,
                attachment_failures=attachment_failures,
            )

            for handler in self._handlers:
                try:
                    await handler(mx_msg)
                except Exception as e:
                    log_event(
                        logger,
                        logging.ERROR,
                        "max.inbound.handler_failed",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="dispatch",
                        outcome="failed",
                        max_chat_id=chat_id,
                        max_msg_id=msg_id,
                        error=str(e),
                    )

        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "max.inbound.failed",
                stage="received",
                outcome="failed",
                reason="message_parse_failed",
                error=str(e),
            )

    def _build_failfast_interactive_ping(self, client, *, ping_interval: float,
                                         failure_limit: int, ping_opcode,
                                         disconnect_error):
        """Создать ping loop, который форсирует reconnect после серии ошибок.

        Upstream pymax логирует `Interactive ping failed`, но сам reconnect не
        инициирует. В результате сокет может висеть в полуживом состоянии
        несколько минут и терять входящие события. Здесь после N подряд ошибок
        мы закрываем клиента и отдаём управление нашему outer reconnect loop.
        """
        normalized_interval = max(0.0, float(ping_interval))
        normalized_limit = max(1, int(failure_limit))

        async def _send_interactive_ping() -> None:
            consecutive_failures = 0

            while getattr(client, "is_connected", False):
                try:
                    await client._send_and_wait(
                        opcode=ping_opcode,
                        payload={"interactive": True},
                        cmd=0,
                    )
                    if consecutive_failures:
                        client.logger.info(
                            "Interactive ping recovered after %s failure(s)",
                            consecutive_failures,
                        )
                    consecutive_failures = 0
                    client.logger.debug("Interactive ping sent successfully")
                except disconnect_error:
                    client.logger.debug("Socket disconnected, exiting ping loop")
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_failures += 1
                    client.logger.warning(
                        "Interactive ping failed (%s/%s): %s",
                        consecutive_failures,
                        normalized_limit,
                        exc,
                    )
                    if consecutive_failures >= normalized_limit:
                        client.logger.error(
                            "Interactive ping failure limit reached (%s), forcing reconnect",
                            normalized_limit,
                        )
                        try:
                            await client.close()
                        except Exception:
                            client.logger.exception(
                                "Failed to close MAX client after ping failure limit"
                            )
                        break

                await asyncio.sleep(normalized_interval)

        return _send_interactive_ping

    def _install_failfast_interactive_ping(self, client):
        try:
            from pymax.exceptions import SocketNotConnectedError
            from pymax.static.constant import DEFAULT_PING_INTERVAL
            from pymax.static.enum import Opcode
        except Exception as e:
            logger.warning("Could not install fail-fast interactive ping loop: %s", e)
            return client

        client._send_interactive_ping = self._build_failfast_interactive_ping(
            client,
            ping_interval=DEFAULT_PING_INTERVAL,
            failure_limit=self._interactive_ping_failure_limit,
            ping_opcode=Opcode.PING,
            disconnect_error=SocketNotConnectedError,
        )
        logger.debug(
            "Installed fail-fast interactive ping loop failure_limit=%s interval=%ss",
            self._interactive_ping_failure_limit,
            DEFAULT_PING_INTERVAL,
        )
        return client

    async def _make_client(self):
        """Создать свежий SocketMaxClient (без накопленного кеша)."""
        from pymax import SocketMaxClient
        client = SocketMaxClient(
            phone=self._phone,
            work_dir=self._data_dir,
            session_name=self._session_name,
            reconnect=False,              # управляем reconnect сами
            send_fake_telemetry=False,    # отключаем телеметрию — она вызывает SSL ошибки
        )
        self._wrap_client_stage(client, "_sync")
        self._wrap_client_stage(client, "_login")
        client = self._install_raw_message_interceptor(client)
        return self._install_failfast_interactive_ping(client)

    async def start(self):
        """Запустить клиент с собственным reconnect-циклом.

        reconnect=False в pymax + outer loop: каждый раз создаём свежий клиент,
        чтобы не накапливать кеш dialogs/chats (pymax bug при reconnect=True).
        """
        retry_delay = 5
        first_connect = True

        while True:
            failure_logged = False
            try:
                self._client = await self._make_client()

                async def _on_start():
                    nonlocal first_connect
                    self._started = True
                    self._last_connected_at = int(time.time())
                    self._clear_runtime_issue()
                    log_event(
                        logger,
                        logging.INFO,
                        "max.adapter.connected",
                        stage="startup" if first_connect else "runtime",
                        outcome="connected",
                    )
                    # Получаем ID собственного аккаунта для фильтрации эхо
                    try:
                        me = self._client.me
                        if me:
                            self._own_id = str(getattr(me, "id", None) or "")
                        else:
                            log_event(
                                logger,
                                logging.WARNING,
                                "max.adapter.own_id_missing",
                                stage="startup",
                                outcome="warning",
                            )
                    except Exception as e:
                        log_event(
                            logger,
                            logging.WARNING,
                            "max.adapter.own_id_failed",
                            stage="startup",
                            outcome="warning",
                            error=str(e),
                        )

                    self._start_pending_empty_recovery_worker()

                    if first_connect:
                        first_connect = False
                        for h in self._start_handlers:
                            try:
                                await h()
                            except Exception as e:
                                log_event(
                                    logger,
                                    logging.ERROR,
                                    "max.adapter.start_handler_failed",
                                    stage="startup",
                                    outcome="failed",
                                    error=str(e),
                                )
                    else:
                        log_event(
                            logger,
                            logging.INFO,
                            "max.adapter.reconnected",
                            stage="runtime",
                            outcome="connected",
                        )

                self._client.on_start(_on_start)
                if hasattr(self._client, "on_raw_receive"):
                    self._client.on_raw_receive(self._handle_raw_receive)
                    log_event(
                        logger,
                        logging.INFO,
                        "max.raw.handler_registered",
                        stage="startup" if first_connect else "runtime",
                        outcome="registered",
                        raw_handler_count=len(getattr(self._client, "_on_raw_receive_handlers", []) or []),
                    )
                self._client.on_message()(self._handle_raw_message)
                self._client.on_message_edit()(self._handle_raw_message)
                self._client.on_message_delete()(self._handle_raw_message)

                log_event(
                    logger,
                    logging.INFO,
                    "max.adapter.starting",
                    stage="startup" if first_connect else "runtime",
                    outcome="started",
                    phone=mask_phone(self._phone),
                )
                await self._client.start()

                if not self._started and self._last_start_error:
                    issue = self._last_issue
                    log_event(
                        logger,
                        logging.ERROR,
                        "max.adapter.failed",
                        stage="runtime",
                        outcome="failed",
                        reason="client_error",
                        error=self._last_start_error,
                        issue_kind=issue.kind if issue is not None else None,
                        requires_reauth=issue.requires_reauth if issue is not None else False,
                    )
                    failure_logged = True
            except Exception as e:
                if self._last_start_error != (str(e).strip() or e.__class__.__name__):
                    await self._capture_runtime_error(e)
                issue = self._last_issue
                log_event(
                    logger,
                    logging.ERROR,
                    "max.adapter.failed",
                    stage="runtime",
                    outcome="failed",
                    reason="client_error",
                    error=self._last_start_error,
                    issue_kind=issue.kind if issue is not None else None,
                    requires_reauth=issue.requires_reauth if issue is not None else False,
                )
                failure_logged = True

            # Клиент завершился — ждём перед перезапуском
            if not failure_logged and not self._started and self._last_start_error:
                issue = self._last_issue
                log_event(
                    logger,
                    logging.ERROR,
                    "max.adapter.failed",
                    stage="runtime",
                    outcome="failed",
                    reason="client_error",
                    error=self._last_start_error,
                    issue_kind=issue.kind if issue is not None else None,
                    requires_reauth=issue.requires_reauth if issue is not None else False,
                )
            log_event(
                logger,
                logging.INFO,
                "max.adapter.reconnecting",
                stage="runtime",
                outcome="retrying",
                retry_in_seconds=retry_delay,
            )
            self._started = False
            await asyncio.sleep(retry_delay)

    def is_ready(self) -> bool:
        return self._started
