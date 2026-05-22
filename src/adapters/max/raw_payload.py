from __future__ import annotations

from .deps import RawPayloadDeps
from .raw import (
    AttachmentInspectorProxy,
    EmptyRecoveryCandidateBuilder,
    RawHistoryCache,
    RawHistoryFetcher,
    RawPayloadParser,
    RawPayloadTelemetry,
)


class MaxRawPayloadService:
    """Compatibility facade for raw MAX payload parsing/recovery helpers."""

    def __init__(self, deps: RawPayloadDeps):
        self._deps = deps
        attachments = AttachmentInspectorProxy(lambda: self._deps.media)
        self._parser = RawPayloadParser(backend=deps.backend, attachments=attachments)
        self._history = RawHistoryCache(raw_history=deps.raw_history, parser=self._parser)
        self._history_fetcher = RawHistoryFetcher(
            connection=deps.connection,
            backend=deps.backend,
            parser=self._parser,
            cache=self._history,
        )
        self._recovery = EmptyRecoveryCandidateBuilder(
            parser=self._parser,
            attachments=attachments,
        )
        self._telemetry = RawPayloadTelemetry(
            parser=self._parser,
            history=self._history,
            attachments=attachments,
            backend=deps.backend,
        )

    def _extract_reply_to_msg_id(self, message):
        return self._parser._extract_reply_to_msg_id(message)

    def _extract_forwarded_payload(self, message):
        return self._parser._extract_forwarded_payload(message)

    def _object_field_names(self, value):
        return self._parser._object_field_names(value)

    def _object_text_len(self, value):
        return self._parser._object_text_len(value)

    def _object_attach_count(self, value):
        return self._parser._object_attach_count(value)

    def _safe_message_structure_summary(self, value):
        return self._parser._safe_message_structure_summary(value)

    def _render_unknown_message_details(self, **kwargs):
        return self._telemetry._render_unknown_message_details(**kwargs)

    def _cleanup_raw_unwrapped_state(self):
        return self._history._cleanup_raw_unwrapped_state()

    def _remember_expected_raw_history_message(self, chat_id: str, msg_id: str):
        return self._history._remember_expected_raw_history_message(chat_id, msg_id)

    def _expected_raw_history_chat_id(self, msg_id: object):
        return self._history._expected_raw_history_chat_id(msg_id)

    def _mark_raw_unwrapped_message(self, chat_id: str, msg_id: str):
        return self._history._mark_raw_unwrapped_message(chat_id, msg_id)

    def _consume_raw_unwrapped_message(self, chat_id: str, msg_id: str):
        return self._history._consume_raw_unwrapped_message(chat_id, msg_id)

    def _mark_raw_processed_message(self, chat_id: str, msg_id: str):
        return self._history._mark_raw_processed_message(chat_id, msg_id)

    def _is_raw_processed_message(self, chat_id: str, msg_id: str):
        return self._history._is_raw_processed_message(chat_id, msg_id)

    def _payload_value(self, data: dict, *keys: str):
        return self._parser._payload_value(data, *keys)

    def _raw_opcode_name(self, opcode):
        return self._telemetry._raw_opcode_name(opcode)

    def _is_safe_field_name(self, name: object):
        return self._parser._is_safe_field_name(name)

    def _safe_field_paths(self, value, *, max_depth: int = 2, max_items: int = 80):
        return self._parser._safe_field_paths(value, max_depth=max_depth, max_items=max_items)

    def _normalize_message_dict(self, data: dict):
        return self._parser._normalize_message_dict(data)

    def _normalize_raw_media_fields(self, message: dict):
        return self._parser._normalize_raw_media_fields(message)

    def _message_dict_has_content(self, message: dict):
        return self._parser._message_dict_has_content(message)

    def _message_object_has_content(self, message):
        return self._parser._message_object_has_content(message)

    def _raw_attachment_types_from_message_dict(self, message: dict):
        return self._parser._raw_attachment_types_from_message_dict(message)

    def _payload_message_dict(self, payload: dict):
        return self._parser._payload_message_dict(payload)

    def _raw_payload_message_identity(self, payload: dict):
        return self._parser._raw_payload_message_identity(payload)

    def _find_nested_message_dict(self, wrapper: dict):
        return self._parser._find_nested_message_dict(wrapper)

    def _message_object_from_dict(self, message: dict, chat_id, *, prefer_raw: bool = False):
        return self._parser._message_object_from_dict(message, chat_id, prefer_raw=prefer_raw)

    def _cache_raw_history_payload(self, payload: dict):
        return self._history._cache_raw_history_payload(payload)

    def _get_cached_raw_history_message(self, chat_id: str, msg_id: str):
        return self._history._get_cached_raw_history_message(chat_id, msg_id)

    def _raw_history_message_dicts(self, payload: dict):
        return self._parser._raw_history_message_dicts(payload)

    def _find_raw_history_message_dict(self, payload: dict, msg_id: str):
        return self._parser._find_raw_history_message_dict(payload, msg_id)

    async def _fetch_raw_history_payload(self, **kwargs):
        return await self._history_fetcher._fetch_raw_history_payload(**kwargs)

    def _prepare_empty_recovery_candidate(self, candidate, **kwargs):
        return self._recovery._prepare_empty_recovery_candidate(candidate, **kwargs)

    def _build_unwrapped_channel_message(self, payload: dict):
        return self._parser._build_unwrapped_channel_message(payload)

    def _build_raw_regular_message(self, payload: dict):
        return self._parser._build_raw_regular_message(payload)

    def _log_raw_message_missing_chat_id(self, payload: dict):
        return self._telemetry._log_raw_message_missing_chat_id(payload)

    def _log_raw_empty_message(self, payload: dict):
        return self._telemetry._log_raw_empty_message(payload)

    def _raw_payload_identity_hints(self, payload: dict):
        return self._telemetry._raw_payload_identity_hints(payload)

    def _log_raw_unhandled_message_payload(self, payload: dict):
        return self._telemetry._log_raw_unhandled_message_payload(payload)

    def _log_raw_auxiliary_event(self, data: dict):
        return self._telemetry._log_raw_auxiliary_event(data)

    def _log_typed_empty_message(self, **kwargs):
        return self._telemetry._log_typed_empty_message(**kwargs)
