from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Optional

from . import constants as max_constants
from . import payload as max_payload
from .types import ForwardedPayload
from ...bridge.contracts import MAX_PROBABLE_CLIENT_CID_MIN, is_probable_client_cid
from ...logging_utils import build_max_flow_id, log_event

logger = logging.getLogger("src.adapters.max_adapter")


class MaxRawPayloadMixin:
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
            time.monotonic() + max_constants.get("MAX_RAW_HISTORY_EXPECTED_TTL_SECONDS"),
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
        return max_payload.payload_value(data, *keys)

    def _raw_opcode_name(self, opcode) -> Optional[str]:
        opcode_value = getattr(opcode, "value", opcode)
        try:
            from pymax.static.enum import Opcode

            return Opcode(opcode_value).name
        except Exception:
            return str(getattr(opcode, "name", "") or "") or None

    def _is_safe_field_name(self, name: object) -> bool:
        return max_payload.is_safe_field_name(name)

    def _safe_field_paths(self, value, *, max_depth: int = 2, max_items: int = 80) -> list[str]:
        return max_payload.safe_field_paths(
            value,
            max_depth=max_depth,
            max_items=max_items,
        )

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
                "token",
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
                now + max_constants.get("MAX_RAW_HISTORY_CACHE_TTL_SECONDS"),
                message_obj,
            )
            cached += 1

        if len(self._raw_history_messages) > max_constants.get("MAX_RAW_HISTORY_CACHE_SIZE"):
            newest = sorted(
                self._raw_history_messages.items(),
                key=lambda item: item[1][0],
                reverse=True,
            )[:max_constants.get("MAX_RAW_HISTORY_CACHE_SIZE")]
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
