from __future__ import annotations

import logging

from ....logging_utils import log_event
from .inspection import AttachmentInspector
from .parser import RawPayloadParser

logger = logging.getLogger("src.adapters.max_adapter")


class EmptyRecoveryCandidateBuilder:
    def __init__(self, *, parser: RawPayloadParser, attachments: AttachmentInspector):
        self._parser = parser
        self._attachments = attachments

    def _candidate_has_recoverable_content(self, candidate) -> bool:
        if self._parser._message_object_has_content(candidate):
            return True
        forwarded = self._parser._extract_forwarded_payload(candidate)
        return bool(
            forwarded
            and forwarded.message is not None
            and self._parser._message_object_has_content(forwarded.message)
        )

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
            normalized = self._parser._normalize_message_dict(candidate)
            unwrapped = self._parser._build_unwrapped_channel_message(
                {
                    "chatId": chat_id_int,
                    "message": normalized,
                }
            )
            if unwrapped is not None:
                candidate = unwrapped
            else:
                candidate = self._parser._message_object_from_dict(
                    normalized,
                    chat_id,
                    prefer_raw=True,
                )

        if not self._candidate_has_recoverable_content(candidate):
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
                message_fields=self._attachments.safe_attachment_field_names(candidate),
                **self._parser._safe_message_structure_summary(candidate),
            )
            return None

        setattr(candidate, "_from_empty_recovery", True)
        candidate_chat_id = getattr(candidate, "chat_id", None)
        if candidate_chat_id is None:
            setattr(candidate, "chat_id", chat_id_int)
        attaches = getattr(candidate, "attaches", None) or []
        attach_list = attaches if isinstance(attaches, list) else [attaches]
        attachment_types = [
            self._attachments.normalize_attachment_type(
                self._attachments.attachment_type_name(attach)
            )
            for attach in attach_list
            if attach is not None and self._attachments.attachment_type_name(attach)
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
