from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

from ..types import ForwardedPayload
from ....logging_utils import build_max_flow_id, log_event
from .history import RawHistoryCache
from .inspection import AttachmentInspector
from .parser import RawPayloadParser

logger = logging.getLogger("src.adapters.max_adapter")


class RawPayloadTelemetry:
    def __init__(
        self,
        *,
        parser: RawPayloadParser,
        history: RawHistoryCache,
        attachments: AttachmentInspector,
        backend,
    ):
        self._parser = parser
        self._history = history
        self._attachments = attachments
        self._backend = backend

    def _raw_opcode_name(self, opcode) -> Optional[str]:
        return self._backend.opcode_name(opcode)

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
            ("outer_text_len", self._parser._object_text_len(message)),
            ("content_text_len", self._parser._object_text_len(content_message)),
            ("outer_attach_count", self._parser._object_attach_count(message)),
            ("content_attach_count", self._parser._object_attach_count(content_message)),
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

        outer_fields = self._parser._object_field_names(message)
        content_fields = self._parser._object_field_names(content_message)
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

    def _log_raw_message_missing_chat_id(self, payload: dict):
        message, _outer_chat_id = self._parser._payload_message_dict(payload)
        if not message or not self._parser._message_dict_has_content(message):
            return

        msg_id = self._parser._payload_value(message, "id", "messageId", "message_id", "msgId")
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
            message_type=str(self._parser._payload_value(message, "type") or "") or None,
            payload_fields=self._attachments.safe_attachment_field_names(SimpleNamespace(**payload)),
            message_fields=self._attachments.safe_attachment_field_names(SimpleNamespace(**message)),
            raw_attachment_types=self._parser._raw_attachment_types_from_message_dict(message),
        )

    def _log_raw_empty_message(self, payload: dict):
        message, outer_chat_id = self._parser._payload_message_dict(payload)
        if not message:
            return

        if self._parser._message_dict_has_content(message):
            return

        message_type = str(self._parser._payload_value(message, "type") or "").upper()
        if message_type not in {"", "TEXT", "USER"}:
            return

        msg_id = self._parser._payload_value(message, "id", "messageId", "message_id")
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
            payload_fields=self._attachments.safe_attachment_field_names(SimpleNamespace(**payload)),
            message_fields=self._attachments.safe_attachment_field_names(SimpleNamespace(**message)),
            raw_attachment_types=self._parser._raw_attachment_types_from_message_dict(message),
        )

    def _raw_payload_identity_hints(self, payload: dict) -> tuple[object, object]:
        message, outer_chat_id = self._parser._payload_message_dict(payload)
        if message:
            msg_id = self._parser._payload_value(message, "id", "messageId", "message_id", "msgId")
            chat_id = (
                self._parser._payload_value(message, "chatId", "chat_id")
                or outer_chat_id
                or self._history._expected_raw_history_chat_id(msg_id)
            )
            return chat_id, msg_id

        messages = self._parser._payload_value(payload, "messages")
        if isinstance(messages, list):
            for raw_message in messages:
                if not isinstance(raw_message, dict):
                    continue
                message = self._parser._normalize_message_dict(raw_message)
                msg_id = self._parser._payload_value(
                    message,
                    "id",
                    "messageId",
                    "message_id",
                    "msgId",
                )
                chat_id = (
                    self._parser._payload_value(message, "chatId", "chat_id")
                    or self._history._expected_raw_history_chat_id(msg_id)
                )
                if chat_id is not None or msg_id is not None:
                    return chat_id, msg_id

        chat_id = self._parser._payload_value(payload, "chatId", "chat_id")
        msg_id = self._parser._payload_value(payload, "messageId", "message_id", "msgId", "id")
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
            payload_fields=self._attachments.safe_attachment_field_names(SimpleNamespace(**payload)),
            payload_shape=self._parser._safe_field_paths(payload),
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
        payload_shape = self._parser._safe_field_paths(payload)
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
            payload_fields=self._attachments.safe_attachment_field_names(SimpleNamespace(**payload)),
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
            message_fields=self._attachments.safe_attachment_field_names(message),
            content_fields=self._attachments.safe_attachment_field_names(content_message),
            **self._parser._safe_message_structure_summary(content_message),
        )
