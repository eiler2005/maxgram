from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from .. import payload as max_payload
from ..types import ForwardedPayload
from ....bridge.contracts import is_probable_client_cid
from .inspection import AttachmentInspector


class RawPayloadParser:
    def __init__(self, *, backend, attachments: AttachmentInspector):
        self._backend = backend
        self._attachments = attachments

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

    def _payload_value(self, data: dict, *keys: str):
        return max_payload.payload_value(data, *keys)

    def _is_safe_field_name(self, name: object) -> bool:
        return max_payload.is_safe_field_name(name)

    def _safe_field_paths(self, value, *, max_depth: int = 2, max_items: int = 80) -> list[str]:
        return max_payload.safe_field_paths(value, max_depth=max_depth, max_items=max_items)

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
        if not any(
            key in message
            for key in ("id", "messageId", "message_id", "msgId", "sender", "text", "attaches", "attachments")
        ):
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
                return self._backend.make_message_from_dict(payload)
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

        chat_id = self._payload_value(message, "chatId", "chat_id") or outer_chat_id
        if chat_id is None or is_probable_client_cid(chat_id):
            return None
        message_obj = self._message_object_from_dict(message, str(chat_id), prefer_raw=True)
        setattr(message_obj, "_from_raw_unwrapped", True)
        return message_obj
