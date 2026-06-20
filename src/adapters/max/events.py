from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import msgpack

from ...bridge.contracts import (
    MaxAttachment,
    MaxAttachmentFailure,
    MaxMessage,
    MaxMessageAction,
    MaxReactionUpdate,
    MaxTypingEvent,
    is_probable_client_cid,
    is_usable_max_chat_id,
)
from .deps import EventsDeps
from . import constants as max_constants
from ...logging_utils import build_max_flow_id, log_event, sanitize_path

logger = logging.getLogger("src.adapters.max_adapter")

_CHANNEL_METADATA_MESSAGE_TYPES = {"CHANNEL", "FORWARD", "FORWARDED"}
_CHANNEL_METADATA_ONLY_FIELDS = {
    "attaches",
    "attachments",
    "chatId",
    "chat_id",
    "cid",
    "elements",
    "id",
    "link",
    "mark",
    "messageId",
    "message_id",
    "options",
    "prevMessageId",
    "prev_message_id",
    "reactionInfo",
    "reaction_info",
    "sender",
    "stats",
    "status",
    "text",
    "time",
    "ttl",
    "type",
    "unread",
}
_CHANNEL_METADATA_MARKER_FIELDS = {
    "cid",
    "mark",
    "options",
    "prevMessageId",
    "prev_message_id",
    "reactionInfo",
    "reaction_info",
    "stats",
    "unread",
}
_CONTROL_USER_LIST_KEYS = (
    "user_ids",
    "userIds",
    "users",
    "members",
    "participants",
    "contacts",
)
_CONTROL_USER_OBJECT_KEYS = (
    "target",
    "target_user",
    "targetUser",
    "target_member",
    "targetMember",
    "user",
    "member",
    "participant",
    "contact",
)
_CONTROL_USER_ID_KEYS = (
    "user_id",
    "userId",
    "member_id",
    "memberId",
    "participant_id",
    "participantId",
    "contact_id",
    "contactId",
    "account_id",
    "accountId",
)
_USER_ID_KEYS = _CONTROL_USER_ID_KEYS + ("id",)
_REACTION_ACTOR_OBJECT_KEYS = (
    "actor",
    "author",
    "sender",
    "user",
    "member",
    "participant",
)
_REACTION_ACTOR_ID_KEYS = (
    "actor_id",
    "actorId",
    "author_id",
    "authorId",
    "sender_id",
    "senderId",
    "user_id",
    "userId",
    "member_id",
    "memberId",
    "participant_id",
    "participantId",
)
_REACTION_VALUE_KEYS = ("reaction", "emoji", "reaction_id", "reactionId")
_ACTION_URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
_ACTION_LINK_LIKE_TYPES = {
    "SHARE",
    "INLINE_KEYBOARD",
    "KEYBOARD",
    "BUTTONS",
    "BUTTON",
}
_ACTION_LABEL_KEYS = {
    "label",
    "text",
    "title",
    "name",
    "caption",
    "button_text",
    "buttonText",
}
_ACTION_URL_KEYS = {
    "url",
    "href",
    "link",
    "target",
    "web_app_url",
    "webAppUrl",
}


class MaxEventsService:
    def __init__(self, deps: EventsDeps):
        self._deps = deps

    @property
    def _client(self):
        return self._deps.connection.client

    @property
    def _own_id(self):
        return self._deps.connection.own_id

    @property
    def _handlers(self):
        return self._deps.handlers

    @property
    def _raw_payload(self):
        return self._deps.raw_payload

    @property
    def _media(self):
        return self._deps.media

    @property
    def _resolver(self):
        return self._deps.resolver

    @property
    def _runtime(self):
        return self._deps.runtime

    @property
    def _voice_recovery(self):
        return self._deps.voice_recovery

    @staticmethod
    def _normalize_text(value) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, bytes):
            msgpack_text = MaxEventsService._extract_msgpack_text(value)
            if msgpack_text:
                return msgpack_text
            try:
                return value.decode("utf-8") or None
            except UnicodeDecodeError:
                return None
        if isinstance(value, str):
            return value
        return str(value)

    @staticmethod
    def _normalize_status(value) -> str | None:
        if value is None or value == "":
            return None
        raw = getattr(value, "name", None) or getattr(value, "value", None) or value
        text = str(raw).strip().upper()
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        return text or None

    @staticmethod
    def _extract_msgpack_text(value: bytes) -> str | None:
        payload = MaxEventsService._extract_msgpack_payload(value)
        if payload is None:
            return None
        return MaxEventsService._find_text_value(payload)

    @staticmethod
    def _extract_msgpack_payload(value) -> object | None:
        if not isinstance(value, bytes):
            return None
        try:
            return msgpack.unpackb(value, raw=False, strict_map_key=False)
        except Exception:
            return None

    @staticmethod
    def _find_text_value(value) -> str | None:
        if isinstance(value, dict):
            direct = value.get("text")
            if isinstance(direct, str) and direct:
                return direct
            for key in ("message", "msg", "content"):
                nested = MaxEventsService._find_text_value(value.get(key))
                if nested:
                    return nested
        return None

    @staticmethod
    def _clean_action_url(value: str) -> str | None:
        url = value.strip().rstrip(".,;:!?)]}\u00bb\u201d'")
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if not parsed.netloc:
            return None
        return url

    @staticmethod
    def _is_max_join_url(url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split("@")[-1].split(":")[0]
        return host == "max.ru" and parsed.path.startswith("/join/")

    @staticmethod
    def _domain_label(url: str) -> str:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split("@")[-1].split(":")[0]
        return f"Открыть {host}" if host else "Открыть сайт"

    @staticmethod
    def _sanitize_action_label(label: str | None, *, fallback: str) -> str:
        text = (label or "").strip()
        if not text or _ACTION_URL_RE.search(text):
            return fallback
        text = " ".join(text.split())
        if len(text) > 40:
            text = f"{text[:37].rstrip()}..."
        return text

    def _value_public_fields(self, value) -> dict[str, object]:
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items() if not str(k).startswith("_")}
        raw_fields = getattr(value, "__dict__", None)
        if isinstance(raw_fields, dict):
            return {str(k): v for k, v in raw_fields.items() if not str(k).startswith("_")}
        return {}

    def _text_label_from_fields(self, fields: dict[str, object]) -> str | None:
        for key, value in fields.items():
            if key not in _ACTION_LABEL_KEYS:
                continue
            if isinstance(value, str) and value.strip() and not _ACTION_URL_RE.search(value):
                return value.strip()
        return None

    def _action_from_url(
        self,
        url: str,
        *,
        source_type: str | None,
        label: str | None = None,
    ) -> MaxMessageAction | None:
        clean_url = self._clean_action_url(url)
        if not clean_url:
            return None
        if self._is_max_join_url(clean_url):
            return MaxMessageAction(
                kind="max_join",
                label="Вступить в MAX",
                url=clean_url,
                source_type=source_type,
            )
        fallback = self._domain_label(clean_url)
        return MaxMessageAction(
            kind="open_url",
            label=self._sanitize_action_label(label, fallback=fallback),
            url=clean_url,
            source_type=source_type,
        )

    def _extract_actions_from_text(
        self,
        text: str | None,
        *,
        source_type: str | None,
    ) -> list[MaxMessageAction]:
        if not text:
            return []
        actions: list[MaxMessageAction] = []
        for match in _ACTION_URL_RE.finditer(text):
            action = self._action_from_url(
                match.group(0),
                source_type=source_type,
            )
            if action:
                actions.append(action)
        return actions

    def _extract_actions_from_value(
        self,
        value,
        *,
        source_type: str | None,
        inherited_label: str | None = None,
        max_depth: int = 6,
    ) -> list[MaxMessageAction]:
        actions: list[MaxMessageAction] = []
        stack: list[tuple[object, int, str | None]] = [(value, 0, inherited_label)]
        seen: set[int] = set()
        while stack:
            current, depth, parent_label = stack.pop()
            if current is None or depth > max_depth:
                continue
            if isinstance(current, str):
                actions.extend(
                    self._extract_actions_from_text(
                        current,
                        source_type=source_type,
                    )
                )
                continue
            if isinstance(current, bytes):
                payload = self._extract_msgpack_payload(current)
                if payload is not None:
                    stack.append((payload, depth + 1, parent_label))
                continue
            if isinstance(current, (int, float, bool)):
                continue
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)

            if isinstance(current, (list, tuple, set)):
                for item in current:
                    stack.append((item, depth + 1, parent_label))
                continue

            fields = self._value_public_fields(current)
            if not fields:
                continue
            label = self._text_label_from_fields(fields) or parent_label
            for key, item in fields.items():
                if key in _ACTION_URL_KEYS and isinstance(item, str):
                    action = self._action_from_url(
                        item,
                        source_type=source_type,
                        label=label,
                    )
                    if action:
                        actions.append(action)
                stack.append((item, depth + 1, label))
        return actions

    def _dedupe_actions(self, actions: list[MaxMessageAction]) -> list[MaxMessageAction]:
        deduped: list[MaxMessageAction] = []
        seen: set[tuple[str, str]] = set()
        for action in actions:
            key = (action.kind, action.url)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(action)
        return deduped

    async def _handle_raw_receive(self, data: dict):
        """Перехватить channel wrappers до потери вложенного контента в pymax."""
        raw_opcode = data.get("opcode") if isinstance(data, dict) else None
        opcode_value = getattr(raw_opcode, "value", raw_opcode)
        if not isinstance(data, dict):
            return
        if int(opcode_value or 0) != 128:
            if int(opcode_value or 0) == 49:
                self._raw_payload._cache_raw_history_payload(data.get("payload") or {})
            self._raw_payload._log_raw_auxiliary_event(data)
            return

        payload = data.get("payload") or {}
        identity = self._raw_payload._raw_payload_message_identity(payload)
        if identity and self._raw_payload._is_raw_processed_message(*identity):
            return
        if identity:
            self._raw_payload._mark_raw_processed_message(*identity)

        unwrapped = self._raw_payload._build_unwrapped_channel_message(payload)
        if unwrapped is None:
            regular = self._raw_payload._build_raw_regular_message(payload)
            if regular is None:
                message, _outer_chat_id = self._raw_payload._payload_message_dict(payload)
                if message:
                    if self._raw_payload._message_dict_has_content(message):
                        self._raw_payload._log_raw_message_missing_chat_id(payload)
                    else:
                        self._raw_payload._log_raw_empty_message(payload)
                else:
                    self._raw_payload._log_raw_unhandled_message_payload(payload)
                return

            chat_id = str(getattr(regular, "chat_id", "") or "")
            msg_id = str(getattr(regular, "id", "") or "")
            if chat_id and msg_id:
                self._raw_payload._mark_raw_unwrapped_message(chat_id, msg_id)

            await self._handle_raw_message(regular)
            return

        chat_id = str(getattr(unwrapped, "chat_id", "") or "")
        msg_id = str(getattr(unwrapped, "id", "") or "")
        if chat_id and msg_id:
            self._raw_payload._mark_raw_unwrapped_message(chat_id, msg_id)

        await self._handle_raw_message(unwrapped)

    def _install_raw_message_interceptor(self, client):
        result = client.install_raw_message_interceptor(self._handle_raw_receive)
        if not result.installed:
            log_event(
                logger,
                logging.WARNING,
                "max.raw.interceptor_missing",
                stage="startup",
                outcome="skipped",
                reason=result.reason or "client_has_no_message_notification_handler",
            )
            return client
        log_event(
            logger,
            logging.INFO,
            "max.raw.interceptor_installed",
            stage="startup",
            outcome="installed",
            raw_handler_count=result.raw_handler_count,
        )
        return client

    @staticmethod
    def _normalize_field_key(key: object) -> str:
        return str(key).lower().replace("_", "").replace("-", "")

    def _object_values(self, value) -> dict[str, object]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        raw_fields = getattr(value, "__dict__", None)
        if isinstance(raw_fields, dict):
            data = dict(raw_fields)
            extra = getattr(value, "__pydantic_extra__", None)
            if isinstance(extra, dict):
                data.update(extra)
            return data
        result: dict[str, object] = {}
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
        extra = getattr(value, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            result.update(extra)
        return result

    def _get_field_value(self, source, *keys: str):
        fields = self._object_values(source)
        if not fields:
            return None
        normalized = {
            self._normalize_field_key(k): v
            for k, v in fields.items()
        }
        for key in keys:
            candidate = self._normalize_field_key(key)
            if candidate in normalized:
                return normalized[candidate]
        return None

    def _get_extra_value(self, extra: dict, *keys: str):
        return self._get_field_value(extra, *keys)

    def _coerce_user_ids(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value if v is not None]
        return [str(value)]

    def _text_value(self, value) -> str | None:
        raw = getattr(value, "value", value)
        if raw is None:
            return None
        if isinstance(raw, bool):
            return None
        text = str(raw).strip()
        return text or None

    def _extract_user_id(self, value) -> str | None:
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            return self._text_value(value)
        for key in _USER_ID_KEYS:
            text = self._text_value(self._get_field_value(value, key))
            if text:
                return text
        return None

    def _extract_embedded_user_name(self, value) -> str | None:
        for key in ("display_name", "displayName", "full_name", "fullName"):
            name = self._text_value(self._get_field_value(value, key))
            if name:
                return name

        names = self._get_field_value(value, "names")
        if isinstance(names, (list, tuple)) and names:
            first_item = names[0]
            first = (
                self._text_value(self._get_field_value(first_item, "first_name", "firstName"))
                or self._text_value(self._get_field_value(first_item, "name"))
                or ""
            )
            last = self._text_value(
                self._get_field_value(first_item, "last_name", "lastName")
            ) or ""
            name = f"{first} {last}".strip()
            if name:
                return name

        first = (
            self._text_value(self._get_field_value(value, "first_name", "firstName"))
            or ""
        )
        last = self._text_value(self._get_field_value(value, "last_name", "lastName")) or ""
        name = f"{first} {last}".strip()
        if name:
            return name

        return self._text_value(self._get_field_value(value, "name"))

    def _collect_user_refs_from_value(self, value) -> list[object]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            refs: list[object] = []
            for item in value:
                refs.extend(self._collect_user_refs_from_value(item))
            return refs
        if isinstance(value, dict):
            if self._extract_user_id(value) or self._extract_embedded_user_name(value):
                return [value]
            refs = []
            for key, item in value.items():
                if item is None and self._extract_user_id(key):
                    refs.append(key)
                elif self._extract_user_id(item) or self._extract_embedded_user_name(item):
                    refs.append(item)
            return refs
        return [value] if self._extract_user_id(value) or self._extract_embedded_user_name(value) else []

    def _extract_control_user_refs(self, attach) -> list[object]:
        refs: list[object] = []
        for source in (self._get_field_value(attach, "extra") or {}, attach):
            for key in _CONTROL_USER_LIST_KEYS:
                refs.extend(self._collect_user_refs_from_value(self._get_field_value(source, key)))
            for key in _CONTROL_USER_OBJECT_KEYS:
                refs.extend(self._collect_user_refs_from_value(self._get_field_value(source, key)))
            for key in _CONTROL_USER_ID_KEYS:
                value = self._get_field_value(source, key)
                if value is not None:
                    refs.append(value)

        seen: set[str] = set()
        unique_refs: list[object] = []
        for ref in refs:
            ref_id = self._extract_user_id(ref)
            ref_name = self._extract_embedded_user_name(ref)
            key = ref_id or (f"name:{ref_name}" if ref_name else None)
            if not key or key in seen:
                continue
            seen.add(key)
            unique_refs.append(ref)
        return unique_refs

    async def _render_user_list(self, user_ids: list[str]) -> Optional[str]:
        if not user_ids:
            return None

        names: list[str] = []
        unresolved = 0
        for uid in user_ids:
            name = await self._resolver.resolve_user_name(uid)
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

    async def _render_user_refs(self, refs: list[object]) -> Optional[str]:
        if not refs:
            return None

        names: list[str] = []
        unresolved = 0
        for ref in refs:
            embedded_name = self._extract_embedded_user_name(ref)
            if embedded_name:
                names.append(embedded_name)
                continue
            user_id = self._extract_user_id(ref)
            if user_id:
                name = await self._resolver.resolve_user_name(user_id)
                if name:
                    names.append(name)
                else:
                    unresolved += 1

        if names:
            if unresolved:
                names.append(f"ещё {unresolved}")
            return ", ".join(names)
        if unresolved == 1:
            return "участник"
        if unresolved > 1:
            return f"{unresolved} участников"
        return None

    def _control_fallback_text(self, attach) -> str:
        event = str(self._get_field_value(attach, "event") or "").lower()
        if event in {"joinbylink", "join_by_link", "joinedbylink"}:
            return "Участник присоединился по ссылке"
        if event in {"add", "invite", "join", "joined"}:
            return "В чат добавлен участник"
        if event in {"leave", "left", "exit"}:
            return "Участник вышел из чата"
        if event in {"remove", "removed", "kick"}:
            return "Участник удалён из чата"
        if event in {"new", "create", "created"}:
            return "Создан новый чат"
        if event in {"rename", "title", "theme"}:
            return "Изменено название чата"
        if event in {"description", "about", "profile"}:
            return "Изменён профиль чата"
        return "Системное событие MAX"

    async def _render_control_attach(self, attach, sender_id: Optional[str],
                                     sender_name: Optional[str]) -> Optional[str]:
        event = str(self._get_field_value(attach, "event") or "").lower()
        extra = self._get_field_value(attach, "extra") or {}
        user_ids = self._coerce_user_ids(
            self._get_extra_value(extra, "user_ids", "userIds", "users", "members")
        )
        user_refs = self._extract_control_user_refs(attach)
        rendered_users = (
            await self._render_user_refs(user_refs)
            or await self._render_user_list(user_ids)
        )
        title = self._get_extra_value(extra, "title", "theme", "name")
        actor = sender_name

        if event in {"add", "invite", "join", "joined", "joinbylink", "join_by_link", "joinedbylink"}:
            if event in {"joinbylink", "join_by_link", "joinedbylink"}:
                if rendered_users:
                    return f"Присоединились по ссылке: {rendered_users}"
                # Имя присоединившегося может прийти через sender
                name = actor
                if not name and sender_id:
                    name = await self._resolver.resolve_user_name(sender_id)
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
                resolved_actor = await self._resolver.resolve_user_name(sender_id)
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

    async def _safe_render_non_media_attach(
        self,
        attach,
        *,
        atype: str,
        raw_type: str,
        sender_id: Optional[str],
        sender_name: Optional[str],
        flow_id: str,
        chat_id: str,
        msg_id: str,
    ) -> Optional[str]:
        try:
            if atype == "CONTROL":
                return await self._render_control_attach(attach, sender_id, sender_name)
            if atype == "CONTACT":
                return self._render_contact_attach(attach)
            if atype == "STICKER":
                return self._render_sticker_attach(attach)
            return f"[Вложение MAX: {raw_type.lower()}]" if raw_type else None
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.inbound.service_render_failed",
                flow_id=flow_id,
                direction="inbound",
                stage="normalize",
                outcome="degraded",
                reason="non_media_attachment_render_error",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                attachment_type=atype or raw_type,
                error_type=type(e).__name__,
            )
            if atype == "CONTROL":
                return self._control_fallback_text(attach)
            return f"[Вложение MAX: {raw_type.lower()}]" if raw_type else None

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
            duration=self._media._duration_seconds(
                getattr(attach, "duration", None),
                kind=self._attachment_kind_for_type(atype),
            ),
            width=getattr(attach, "width", None),
            height=getattr(attach, "height", None),
        )

    def _should_skip_empty_event(self, message_type: Optional[str], text: Optional[str],
                                  attachments: list["MaxAttachment"],
                                  rendered_texts: list[str],
                                  actions: list["MaxMessageAction"],
                                  reaction_info,
                                  attachment_failures: list["MaxAttachmentFailure"] | None = None) -> bool:
        if text or attachments or rendered_texts or actions or attachment_failures:
            return False

        normalized_type = str(message_type or "").upper()
        if reaction_info is not None:
            return True

        return normalized_type in {"", "TEXT", "USER"}

    def _is_channel_metadata_only_event(self, message_type: Optional[str], message, content_message) -> bool:
        normalized_type = str(message_type or "").upper()
        if normalized_type not in _CHANNEL_METADATA_MESSAGE_TYPES:
            return False

        fields = set(self._raw_payload._object_field_names(message))
        fields.update(self._raw_payload._object_field_names(content_message))
        if not fields:
            return False
        if fields - _CHANNEL_METADATA_ONLY_FIELDS:
            return False
        return bool(fields & _CHANNEL_METADATA_MARKER_FIELDS)

    def _is_degraded_channel_media_event(
        self,
        *,
        message_type: Optional[str],
        message,
        attachments: list[MaxAttachment],
        rendered_texts: list[str],
        attachment_failures: list[MaxAttachmentFailure],
    ) -> bool:
        normalized_type = str(message_type or "").upper()
        if normalized_type not in _CHANNEL_METADATA_MESSAGE_TYPES:
            return False
        if getattr(message, "_from_raw_unwrapped", False):
            return False
        if getattr(message, "_from_empty_recovery", False):
            return False
        if attachments or rendered_texts or not attachment_failures:
            return False
        return any(
            failure.kind in {"photo", "video", "audio", "document"}
            for failure in attachment_failures
        )

    def _object_value(self, source, *names: str):
        if isinstance(source, dict):
            return self._raw_payload._payload_value(source, *names)
        for name in names:
            if hasattr(source, name):
                value = getattr(source, name, None)
                if value is not None:
                    return value
        return None

    def _content_message_for_media_quality(self, message):
        forwarded = self._raw_payload._extract_forwarded_payload(message)
        if forwarded and forwarded.message is not None:
            return forwarded.message
        return message

    def _attachment_has_usable_media_ref(self, attach, raw_type: str) -> bool:
        normalized_type = self._media._normalize_attachment_type(raw_type)
        if normalized_type == "PHOTO":
            names = ("base_url", "baseUrl", "baseRawUrl", "url", "file_id", "fileId", "id")
        elif normalized_type == "VIDEO":
            names = ("url", "video_id", "videoId", "id")
        elif normalized_type == "AUDIO":
            names = ("url", "audio_id", "audioId", "file_id", "fileId", "id", "wave")
        elif normalized_type in {"FILE", "DOCUMENT"}:
            names = ("url", "file_id", "fileId", "id")
        else:
            return True
        return any(self._object_value(attach, name) is not None for name in names)

    def _media_ref_quality(self, message) -> tuple[bool, bool, list[str]]:
        content_message = self._content_message_for_media_quality(message)
        attaches = self._object_value(content_message, "attaches", "attachments") or []
        attach_list = attaches if isinstance(attaches, list) else [attaches]
        media_types: list[str] = []
        has_low_quality_media = False
        for attach in attach_list:
            if attach is None:
                continue
            raw_type = self._media._attachment_type_name(attach)
            normalized_type = self._media._normalize_attachment_type(raw_type)
            if normalized_type not in {"PHOTO", "VIDEO", "AUDIO", "FILE", "DOCUMENT"}:
                continue
            media_types.append(normalized_type)
            if not self._attachment_has_usable_media_ref(attach, raw_type):
                has_low_quality_media = True
        return bool(media_types), has_low_quality_media, media_types

    def _has_usable_degraded_media_recovery(self, message) -> bool:
        has_media, has_low_quality_media, _media_types = self._media_ref_quality(message)
        return has_media and not has_low_quality_media

    def _log_low_quality_degraded_media_recovery(
        self,
        *,
        flow_id: str,
        chat_id: str,
        raw_msg_id: str,
        reason: str,
        message,
    ):
        _has_media, _has_low_quality, media_types = self._media_ref_quality(message)
        log_event(
            logger,
            logging.INFO,
            "max.inbound.degraded_media_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="skipped",
            reason=reason,
            max_chat_id=chat_id,
            max_msg_id=raw_msg_id,
            attachment_types=media_types,
        )

    async def _process_cached_degraded_media_recovery(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        flow_id: str,
    ) -> bool:
        cached = self._raw_payload._get_cached_raw_history_message(chat_id, raw_msg_id)
        if cached is None:
            return False
        if not self._has_usable_degraded_media_recovery(cached):
            self._log_low_quality_degraded_media_recovery(
                flow_id=flow_id,
                chat_id=chat_id,
                raw_msg_id=raw_msg_id,
                reason="low_quality_cached_recovery",
                message=cached,
            )
            return False
        log_event(
            logger,
            logging.INFO,
            "max.inbound.degraded_media_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="recovered",
            reason="raw_history_cache_match",
            max_chat_id=chat_id,
            max_msg_id=raw_msg_id,
        )
        await self._handle_raw_message(cached)
        return True

    async def _recover_degraded_channel_media_event(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        flow_id: str,
    ) -> bool:
        recovered = await self._voice_recovery._recover_empty_message_from_recent_history(
            chat_id=chat_id,
            raw_msg_id=raw_msg_id,
            flow_id=flow_id,
        )
        if recovered is not None:
            if not self._has_usable_degraded_media_recovery(recovered):
                self._log_low_quality_degraded_media_recovery(
                    flow_id=flow_id,
                    chat_id=chat_id,
                    raw_msg_id=raw_msg_id,
                    reason="low_quality_recovery",
                    message=recovered,
                )
            else:
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.degraded_media_recovery",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="recover",
                    outcome="recovered",
                    reason="recent_history_match",
                    max_chat_id=chat_id,
                    max_msg_id=raw_msg_id,
                )
                await self._handle_raw_message(recovered)
                return True

        if await self._process_cached_degraded_media_recovery(
            chat_id=chat_id,
            raw_msg_id=raw_msg_id,
            flow_id=flow_id,
        ):
            return True

        wait_seconds = float(max_constants.get("MAX_DEGRADED_MEDIA_RECOVERY_WAIT_SECONDS"))
        poll_seconds = float(max_constants.get("MAX_DEGRADED_MEDIA_RECOVERY_POLL_SECONDS"))
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self._raw_payload._is_raw_processed_message(chat_id, raw_msg_id):
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.degraded_media_recovery",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="recover",
                    outcome="skipped",
                    reason="raw_unwrapped_arrived",
                    max_chat_id=chat_id,
                    max_msg_id=raw_msg_id,
                )
                return True
            if await self._process_cached_degraded_media_recovery(
                chat_id=chat_id,
                raw_msg_id=raw_msg_id,
                flow_id=flow_id,
            ):
                return True
            await asyncio.sleep(poll_seconds)

        log_event(
            logger,
            logging.INFO,
            "max.inbound.degraded_media_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="miss",
            reason="raw_unwrapped_timeout",
            max_chat_id=chat_id,
            max_msg_id=raw_msg_id,
        )
        return False

    async def _handle_raw_message(self, message):
        """Конвертируем raw MAX Message → MaxMessage и вызываем handlers.

        MAX client message view fields:
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
                and self._raw_payload._consume_raw_unwrapped_message(chat_id, raw_msg_id)
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

            forwarded = self._raw_payload._extract_forwarded_payload(message)
            content_message = forwarded.message if forwarded else message

            raw_text_value = (
                getattr(content_message, "text", None)
                or getattr(message, "text", None)
                or None
            )
            msgpack_payload = self._extract_msgpack_payload(raw_text_value)
            text = self._normalize_text(raw_text_value)
            message_type = str(
                getattr(content_message, "type", None)
                or getattr(message, "type", None)
                or ""
            ) or None
            status = self._normalize_status(getattr(message, "status", None))
            reaction_info = (
                getattr(message, "reactionInfo", None)
                or getattr(message, "reaction_info", None)
                or getattr(content_message, "reactionInfo", None)
                or getattr(content_message, "reaction_info", None)
            )
            msg_id = f"{raw_msg_id}:{status}" if raw_msg_id and status else raw_msg_id
            source_media_chat_id = (
                forwarded.chat_id
                if forwarded
                else getattr(message, "_forward_source_chat_id", None)
            )
            source_media_msg_id = (
                forwarded.msg_id
                if forwarded
                else getattr(message, "_forward_source_msg_id", None)
            )
            if is_usable_max_chat_id(source_media_chat_id):
                media_chat_id = str(source_media_chat_id)
                media_msg_id = str(source_media_msg_id or raw_msg_id)
            else:
                media_chat_id = chat_id
                media_msg_id = str(source_media_msg_id or raw_msg_id)

            # Отправитель: message.sender — это int
            sender_int = getattr(message, "sender", None)
            sender_id  = str(sender_int) if sender_int is not None else None
            reply_to_msg_id = self._raw_payload._extract_reply_to_msg_id(message)
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
                    message_fields=self._media._safe_attachment_field_names(message),
                    content_fields=self._media._safe_attachment_field_names(content_message),
                    **self._raw_payload._safe_message_structure_summary(content_message),
                )
                return

            is_own = bool(self._own_id and sender_id == self._own_id)
            if is_own:
                if self._runtime._consume_expected_outbound_id(chat_id, raw_msg_id):
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
                pending = self._runtime._claim_pending_outbound_ack(chat_id, text, reply_to_msg_id)
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

            # Название чата: для групп ищем в кеше клиента.
            chat_title = None
            if not is_dm and self._client:
                try:
                    chat_obj = next(
                        (
                            c
                            for c in self._client.group_chats_snapshot()
                            if c.id == chat_id_int
                        ),
                        None,
                    )
                    if chat_obj:
                        chat_title = getattr(chat_obj, "title", None)
                except Exception:
                    pass

            attaches = getattr(content_message, "attaches", None) or []
            attach_list = attaches if isinstance(attaches, list) else [attaches]
            raw_attachment_types = [
                self._media._attachment_type_name(attach)
                for attach in attach_list
                if attach is not None
            ]
            attachment_types = [
                self._media._normalize_attachment_type(atype)
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
                self._raw_payload._log_typed_empty_message(
                    flow_id=flow_id,
                    message=message,
                    content_message=content_message,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    message_type=message_type,
                    reaction_info=reaction_info,
                )
                recovered = await self._voice_recovery._recover_empty_message_from_recent_history(
                    chat_id=chat_id,
                    raw_msg_id=raw_msg_id,
                    flow_id=flow_id,
                )
                if recovered is not None:
                    await self._handle_raw_message(recovered)
                    return
                if self._voice_recovery._schedule_empty_recovery_cache_wait(
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
                self._voice_recovery._forget_pending_empty_recovery(
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
                try:
                    sender_name = await self._resolver.resolve_user_name(sender_id)
                except Exception as e:
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.inbound.sender_resolve_failed",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="normalize",
                        outcome="degraded",
                        reason="sender_name_lookup_error",
                        max_chat_id=chat_id,
                        max_msg_id=msg_id,
                        error_type=type(e).__name__,
                    )

            # own_id сохраняем в msg для фильтрации в BridgeCore
            # (не фильтруем здесь — bridge решает сам)

            # Вложения в MAX client message view.
            attachments: list[MaxAttachment] = []
            attachment_failures: list[MaxAttachmentFailure] = []
            rendered_texts: list[str] = []
            actions: list[MaxMessageAction] = []
            actions.extend(self._extract_actions_from_text(text, source_type="text"))
            if msgpack_payload is not None:
                actions.extend(
                    self._extract_actions_from_value(
                        msgpack_payload,
                        source_type="msgpack_text",
                    )
                )
            media_index = 0
            for attach in attach_list:
                if attach is None:
                    continue
                raw_type = self._media._attachment_type_name(attach)
                raw_type_upper = str(raw_type or "").upper()
                atype = self._media._normalize_attachment_type(raw_type)
                if atype in {"PHOTO", "VIDEO", "AUDIO", "FILE"}:
                    filename_hint = self._media._attachment_filename(attach)
                    attachment = await self._media._download_attachment(
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

                attach_actions = self._extract_actions_from_value(
                    attach,
                    source_type=raw_type,
                )
                if attach_actions:
                    actions.extend(attach_actions)

                link_like = (
                    atype in _ACTION_LINK_LIKE_TYPES
                    or raw_type_upper in _ACTION_LINK_LIKE_TYPES
                )
                if link_like and actions:
                    continue

                rendered = await self._safe_render_non_media_attach(
                    attach,
                    atype=atype,
                    raw_type=raw_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    flow_id=flow_id,
                    chat_id=chat_id,
                    msg_id=msg_id,
                )

                if rendered:
                    rendered_texts.append(rendered)

            if status == "EDITED":
                rendered_texts.insert(0, "[Сообщение отредактировано]")
            elif status == "REMOVED":
                rendered_texts = ["[Сообщение удалено]"]
                actions = []
            actions = self._dedupe_actions(actions)

            if self._is_degraded_channel_media_event(
                message_type=message_type,
                message=message,
                attachments=attachments,
                rendered_texts=rendered_texts,
                attachment_failures=attachment_failures,
            ):
                if await self._recover_degraded_channel_media_event(
                    chat_id=chat_id,
                    raw_msg_id=raw_msg_id,
                    flow_id=flow_id,
                ):
                    return

            if self._should_skip_empty_event(
                message_type,
                text,
                attachments,
                rendered_texts,
                actions,
                reaction_info,
                attachment_failures,
            ):
                self._raw_payload._log_typed_empty_message(
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
                    recovered = await self._voice_recovery._recover_empty_message_from_recent_history(
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

            if (
                not text
                and not attachments
                and not rendered_texts
                and not actions
                and not attachment_failures
                and self._is_channel_metadata_only_event(message_type, message, content_message)
            ):
                fields = sorted(
                    set(self._raw_payload._object_field_names(message))
                    | set(self._raw_payload._object_field_names(content_message))
                )
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.skipped",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="normalize",
                    outcome="skipped",
                    reason="channel_metadata_only_event",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    message_type=message_type,
                    metadata_fields=fields,
                )
                return

            if not text and not attachments and not rendered_texts and message_type:
                if not actions and message_type.upper() not in {"TEXT", "USER"}:
                    rendered_texts.append(
                        self._raw_payload._render_unknown_message_details(
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
                action_count=len(actions),
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
                actions=actions,
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

    # ── PyMax 2.2.0 event handlers ────────────────────────────────────────

    async def _handle_typing(self, event) -> None:
        chat_id = str(getattr(event, "chat_id", None) or "")
        user_id = str(getattr(event, "user_id", None) or "")
        if not chat_id:
            return
        typing = MaxTypingEvent(chat_id=chat_id, user_id=user_id)
        for handler in self._deps.typing_handlers:
            try:
                await handler(typing)
            except Exception as e:
                log_event(
                    logger,
                    logging.WARNING,
                    "max.inbound.handler_failed",
                    direction="inbound",
                    stage="dispatch",
                    outcome="failed",
                    reason="typing_handler_error",
                    max_chat_id=chat_id,
                    error=str(e),
                )

    def _extract_reaction_actor_ref(self, event):
        for key in _REACTION_ACTOR_OBJECT_KEYS:
            value = self._get_field_value(event, key)
            if value is not None:
                return value
        for key in _REACTION_ACTOR_ID_KEYS:
            value = self._get_field_value(event, key)
            if value is not None:
                return value
        return None

    def _extract_reaction_value(self, event, counters: list[dict]) -> str | None:
        for key in _REACTION_VALUE_KEYS:
            value = self._text_value(self._get_field_value(event, key))
            if value:
                return value
        if len(counters) == 1:
            return self._text_value(counters[0].get("emoji"))
        return None

    async def _handle_reaction_update(self, event) -> None:
        chat_id = str(self._get_field_value(event, "chat_id", "chatId") or "")
        message_id = str(self._get_field_value(event, "message_id", "messageId") or "")
        total_count = int(self._get_field_value(event, "total_count", "totalCount") or 0)
        raw_counters = self._get_field_value(event, "counters") or []
        counters = [
            {
                "emoji": str(self._get_field_value(c, "emoji", "reaction") or ""),
                "count": int(self._get_field_value(c, "count") or 0),
            }
            for c in raw_counters
        ]
        if not chat_id or not message_id:
            return
        actor_user_id = None
        actor_name = None
        reaction_value = None
        try:
            actor_ref = self._extract_reaction_actor_ref(event)
            actor_user_id = self._extract_user_id(actor_ref)
            actor_name = self._extract_embedded_user_name(actor_ref)
            if actor_user_id and not actor_name and self._resolver is not None:
                actor_name = await self._resolver.resolve_user_name(actor_user_id)
            reaction_value = self._extract_reaction_value(event, counters)
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.inbound.reaction_enrichment_failed",
                direction="inbound",
                stage="normalize",
                outcome="degraded",
                reason="reaction_actor_enrichment_error",
                max_chat_id=chat_id,
                max_msg_id=message_id,
                error_type=type(e).__name__,
            )
        reaction = MaxReactionUpdate(
            chat_id=chat_id,
            message_id=message_id,
            total_count=total_count,
            counters=counters,
            actor_user_id=actor_user_id,
            actor_name=actor_name,
            reaction=reaction_value,
        )
        for handler in self._deps.reaction_update_handlers:
            try:
                await handler(reaction)
            except Exception as e:
                log_event(
                    logger,
                    logging.WARNING,
                    "max.inbound.handler_failed",
                    direction="inbound",
                    stage="dispatch",
                    outcome="failed",
                    reason="reaction_update_handler_error",
                    max_chat_id=chat_id,
                    max_msg_id=message_id,
                    error=str(e),
                )

    async def _handle_message_read(self, event) -> None:
        chat_id = str(getattr(event, "chat_id", None) or "")
        user_id = str(getattr(event, "user_id", None) or "")
        mark = getattr(event, "mark", None)
        log_event(
            logger,
            logging.DEBUG,
            "max.inbound.message_read",
            direction="inbound",
            stage="received",
            outcome="noted",
            max_chat_id=chat_id,
            user_id=user_id,
            mark=mark,
        )

    async def _handle_presence(self, event) -> None:
        user_id = str(getattr(event, "user_id", None) or "")
        presence = getattr(event, "presence", None)
        status = str(getattr(presence, "online", None) or "")
        log_event(
            logger,
            logging.DEBUG,
            "max.inbound.presence",
            direction="inbound",
            stage="received",
            outcome="noted",
            user_id=user_id,
            online=status,
        )
