from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aiohttp import ClientSession

from .backends.base import MaxBackend
from .context import MaxAdapterContext
from .state import ConnectionState, EmptyRecoveryState, OutboundState, RawHistoryState
from ..max_session_store import MaxSessionStore
from ...bridge.contracts import IssueHandler, MessageHandler


@dataclass
class RuntimeDeps:
    connection: ConnectionState
    outbound: OutboundState
    issue_handlers: list[IssueHandler]


@dataclass
class ResolveDeps:
    connection: ConnectionState


@dataclass
class SendDeps:
    connection: ConnectionState
    outbound: OutboundState
    backend: MaxBackend
    runtime: Any


@dataclass
class RawPayloadDeps:
    connection: ConnectionState
    raw_history: RawHistoryState
    backend: MaxBackend
    media: Any | None = None


@dataclass
class MediaDeps:
    connection: ConnectionState
    backend: MaxBackend
    tmp_dir: Path
    client_session_factory: type[ClientSession]
    raw_payload: Any


@dataclass
class EventsDeps:
    connection: ConnectionState
    outbound: OutboundState
    handlers: list[MessageHandler]
    backend: MaxBackend
    raw_payload: Any
    media: Any
    resolver: Any
    runtime: Any
    voice_recovery: Any | None = None


@dataclass
class VoiceRecoveryDeps:
    connection: ConnectionState
    raw_history: RawHistoryState
    empty_recovery: EmptyRecoveryState
    data_dir: str
    raw_payload: Any
    events: Any | None = None


@dataclass
class RecoveryDeps:
    connection: ConnectionState
    phone: str
    data_dir: str
    session_name: str
    session_store: MaxSessionStore
    resolver: Any


@dataclass
class LifecycleDeps:
    connection: ConnectionState
    backend: MaxBackend
    phone: str
    start_handlers: list[Callable]
    interactive_ping_failure_limit: int
    runtime: Any
    recovery: Any
    events: Any
    voice_recovery: Any


class ExplicitMaxService:
    def __init__(self, deps):
        self._deps = deps

    @property
    def _backend(self):
        return self._deps.backend

    @property
    def _phone(self):
        return self._deps.phone

    @property
    def _data_dir(self):
        return self._deps.data_dir

    @property
    def _session_name(self):
        return self._deps.session_name

    @property
    def _session_store(self):
        return self._deps.session_store

    @property
    def _tmp_dir(self):
        return self._deps.tmp_dir

    @property
    def _context(self) -> MaxAdapterContext:
        return self._deps.context

    @property
    def _client_session_factory(self):
        return self._deps.client_session_factory

    @property
    def _handlers(self):
        return self._deps.handlers

    @property
    def _start_handlers(self):
        return self._deps.start_handlers

    @property
    def _issue_handlers(self):
        return self._deps.issue_handlers

    @property
    def _interactive_ping_failure_limit(self):
        return self._deps.interactive_ping_failure_limit

    @property
    def _client(self):
        return self._deps.connection.client

    @_client.setter
    def _client(self, value):
        self._deps.connection.client = value

    @property
    def _started(self):
        return self._deps.connection.started

    @_started.setter
    def _started(self, value):
        self._deps.connection.started = value

    @property
    def _own_id(self):
        return self._deps.connection.own_id

    @_own_id.setter
    def _own_id(self, value):
        self._deps.connection.own_id = value

    @property
    def _last_start_error(self):
        return self._deps.connection.last_start_error

    @_last_start_error.setter
    def _last_start_error(self, value):
        self._deps.connection.last_start_error = value

    @property
    def _last_issue(self):
        return self._deps.connection.last_issue

    @_last_issue.setter
    def _last_issue(self, value):
        self._deps.connection.last_issue = value

    @property
    def _last_issue_notification_signature(self):
        return self._deps.connection.last_issue_notification_signature

    @_last_issue_notification_signature.setter
    def _last_issue_notification_signature(self, value):
        self._deps.connection.last_issue_notification_signature = value

    @property
    def _last_connected_at(self):
        return self._deps.connection.last_connected_at

    @_last_connected_at.setter
    def _last_connected_at(self, value):
        self._deps.connection.last_connected_at = value

    @property
    def _pending_outbound_acks(self):
        return self._deps.outbound.pending_outbound_acks

    @_pending_outbound_acks.setter
    def _pending_outbound_acks(self, value):
        self._deps.outbound.pending_outbound_acks = value

    @property
    def _expected_outbound_ids(self):
        return self._deps.outbound.expected_outbound_ids

    @_expected_outbound_ids.setter
    def _expected_outbound_ids(self, value):
        self._deps.outbound.expected_outbound_ids = value

    @property
    def _last_outbound_failure(self):
        return self._deps.outbound.last_outbound_failure

    @_last_outbound_failure.setter
    def _last_outbound_failure(self, value):
        self._deps.outbound.last_outbound_failure = value

    @property
    def _raw_unwrapped_message_ids(self):
        return self._deps.raw_history.raw_unwrapped_message_ids

    @_raw_unwrapped_message_ids.setter
    def _raw_unwrapped_message_ids(self, value):
        self._deps.raw_history.raw_unwrapped_message_ids = value

    @property
    def _raw_processed_message_ids(self):
        return self._deps.raw_history.raw_processed_message_ids

    @_raw_processed_message_ids.setter
    def _raw_processed_message_ids(self, value):
        self._deps.raw_history.raw_processed_message_ids = value

    @property
    def _raw_history_messages(self):
        return self._deps.raw_history.raw_history_messages

    @_raw_history_messages.setter
    def _raw_history_messages(self, value):
        self._deps.raw_history.raw_history_messages = value

    @property
    def _expected_raw_history_messages(self):
        return self._deps.raw_history.expected_raw_history_messages

    @_expected_raw_history_messages.setter
    def _expected_raw_history_messages(self, value):
        self._deps.raw_history.expected_raw_history_messages = value

    @property
    def _pending_empty_recovery_tasks(self):
        return self._deps.empty_recovery.pending_empty_recovery_tasks

    @_pending_empty_recovery_tasks.setter
    def _pending_empty_recovery_tasks(self, value):
        self._deps.empty_recovery.pending_empty_recovery_tasks = value

    @property
    def _pending_empty_recoveries(self):
        return self._deps.empty_recovery.pending_empty_recoveries

    @_pending_empty_recoveries.setter
    def _pending_empty_recoveries(self, value):
        self._deps.empty_recovery.pending_empty_recoveries = value

    @property
    def _pending_empty_recovery_worker(self):
        return self._deps.empty_recovery.pending_empty_recovery_worker

    @_pending_empty_recovery_worker.setter
    def _pending_empty_recovery_worker(self, value):
        self._deps.empty_recovery.pending_empty_recovery_worker = value

    @property
    def _history_sweep_diagnostic_log_until(self):
        return self._deps.empty_recovery.history_sweep_diagnostic_log_until

    @_history_sweep_diagnostic_log_until.setter
    def _history_sweep_diagnostic_log_until(self, value):
        self._deps.empty_recovery.history_sweep_diagnostic_log_until = value

    def _normalize_outbound_text(self, *args, **kwargs):
        return self._deps.runtime._normalize_outbound_text(*args, **kwargs)

    def _set_last_outbound_failure(self, *args, **kwargs):
        return self._deps.runtime._set_last_outbound_failure(*args, **kwargs)

    def _clear_runtime_issue(self, *args, **kwargs):
        return self._deps.runtime._clear_runtime_issue(*args, **kwargs)

    def _classify_runtime_error(self, *args, **kwargs):
        return self._deps.runtime._classify_runtime_error(*args, **kwargs)

    def _remember_runtime_issue(self, *args, **kwargs):
        return self._deps.runtime._remember_runtime_issue(*args, **kwargs)

    async def _emit_runtime_issue(self, *args, **kwargs):
        return await self._deps.runtime._emit_runtime_issue(*args, **kwargs)

    def _capture_runtime_error(self, *args, **kwargs):
        return self._deps.runtime._capture_runtime_error(*args, **kwargs)

    def _wrap_client_stage(self, *args, **kwargs):
        return self._deps.runtime._wrap_client_stage(*args, **kwargs)

    def _cleanup_pending_state(self, *args, **kwargs):
        return self._deps.runtime._cleanup_pending_state(*args, **kwargs)

    def _is_retryable_send_error(self, *args, **kwargs):
        return self._deps.runtime._is_retryable_send_error(*args, **kwargs)

    def _remember_expected_outbound_id(self, *args, **kwargs):
        return self._deps.runtime._remember_expected_outbound_id(*args, **kwargs)

    def _consume_expected_outbound_id(self, *args, **kwargs):
        return self._deps.runtime._consume_expected_outbound_id(*args, **kwargs)

    def _claim_pending_outbound_ack(self, *args, **kwargs):
        return self._deps.runtime._claim_pending_outbound_ack(*args, **kwargs)

    def _extract_result_msg_id(self, *args, **kwargs):
        return self._deps.runtime._extract_result_msg_id(*args, **kwargs)

    def _extract_user_name(self, *args, **kwargs):
        return self._deps.resolver._extract_user_name(*args, **kwargs)

    async def resolve_user_name(self, *args, **kwargs):
        return await self._deps.resolver.resolve_user_name(*args, **kwargs)

    async def resolve_chat_title(self, *args, **kwargs):
        return await self._deps.resolver.resolve_chat_title(*args, **kwargs)

    def _recover_session_if_needed(self, *args, **kwargs):
        return self._deps.recovery._recover_session_if_needed(*args, **kwargs)

    def _backup_session_snapshot(self, *args, **kwargs):
        return self._deps.recovery._backup_session_snapshot(*args, **kwargs)

    async def _handle_raw_message(self, *args, **kwargs):
        return await self._deps.events._handle_raw_message(*args, **kwargs)

    async def _handle_raw_receive(self, *args, **kwargs):
        return await self._deps.events._handle_raw_receive(*args, **kwargs)

    def _install_raw_message_interceptor(self, *args, **kwargs):
        return self._deps.events._install_raw_message_interceptor(*args, **kwargs)

    def _start_pending_empty_recovery_worker(self, *args, **kwargs):
        return self._deps.voice_recovery._start_pending_empty_recovery_worker(*args, **kwargs)

    def _remember_pending_empty_recovery(self, *args, **kwargs):
        return self._deps.voice_recovery._remember_pending_empty_recovery(*args, **kwargs)

    def _forget_pending_empty_recovery(self, *args, **kwargs):
        return self._deps.voice_recovery._forget_pending_empty_recovery(*args, **kwargs)

    def _schedule_empty_recovery_cache_wait(self, *args, **kwargs):
        return self._deps.voice_recovery._schedule_empty_recovery_cache_wait(*args, **kwargs)

    async def _recover_empty_message_from_recent_history(self, *args, **kwargs):
        return await self._deps.voice_recovery._recover_empty_message_from_recent_history(
            *args, **kwargs
        )

    def _pending_empty_recovery_ids_for_chat(self, *args, **kwargs):
        return self._deps.voice_recovery._pending_empty_recovery_ids_for_chat(*args, **kwargs)

    def _log_history_sweep_pending_diagnostic(self, *args, **kwargs):
        return self._deps.voice_recovery._log_history_sweep_pending_diagnostic(*args, **kwargs)

    def _pending_empty_recovery_path(self, *args, **kwargs):
        return self._deps.voice_recovery._pending_empty_recovery_path(*args, **kwargs)

    def _pending_empty_recovery_key(self, *args, **kwargs):
        return self._deps.voice_recovery._pending_empty_recovery_key(*args, **kwargs)

    def _save_pending_empty_recoveries(self, *args, **kwargs):
        return self._deps.voice_recovery._save_pending_empty_recoveries(*args, **kwargs)

    def _empty_recovery_retry_delay(self, *args, **kwargs):
        return self._deps.voice_recovery._empty_recovery_retry_delay(*args, **kwargs)

    async def _recover_empty_message_from_raw_history_cache_later(self, *args, **kwargs):
        return await self._deps.voice_recovery._recover_empty_message_from_raw_history_cache_later(
            *args, **kwargs
        )

    def _extract_reply_to_msg_id(self, *args, **kwargs):
        return self._deps.raw_payload._extract_reply_to_msg_id(*args, **kwargs)

    def _extract_forwarded_payload(self, *args, **kwargs):
        return self._deps.raw_payload._extract_forwarded_payload(*args, **kwargs)

    def _object_field_names(self, *args, **kwargs):
        return self._deps.raw_payload._object_field_names(*args, **kwargs)

    def _object_text_len(self, *args, **kwargs):
        return self._deps.raw_payload._object_text_len(*args, **kwargs)

    def _object_attach_count(self, *args, **kwargs):
        return self._deps.raw_payload._object_attach_count(*args, **kwargs)

    def _safe_message_structure_summary(self, *args, **kwargs):
        return self._deps.raw_payload._safe_message_structure_summary(*args, **kwargs)

    def _render_unknown_message_details(self, *args, **kwargs):
        return self._deps.raw_payload._render_unknown_message_details(*args, **kwargs)

    def _remember_expected_raw_history_message(self, *args, **kwargs):
        return self._deps.raw_payload._remember_expected_raw_history_message(*args, **kwargs)

    def _expected_raw_history_chat_id(self, *args, **kwargs):
        return self._deps.raw_payload._expected_raw_history_chat_id(*args, **kwargs)

    def _mark_raw_unwrapped_message(self, *args, **kwargs):
        return self._deps.raw_payload._mark_raw_unwrapped_message(*args, **kwargs)

    def _consume_raw_unwrapped_message(self, *args, **kwargs):
        return self._deps.raw_payload._consume_raw_unwrapped_message(*args, **kwargs)

    def _mark_raw_processed_message(self, *args, **kwargs):
        return self._deps.raw_payload._mark_raw_processed_message(*args, **kwargs)

    def _is_raw_processed_message(self, *args, **kwargs):
        return self._deps.raw_payload._is_raw_processed_message(*args, **kwargs)

    def _payload_value(self, *args, **kwargs):
        return self._deps.raw_payload._payload_value(*args, **kwargs)

    def _safe_field_paths(self, *args, **kwargs):
        return self._deps.raw_payload._safe_field_paths(*args, **kwargs)

    def _normalize_message_dict(self, *args, **kwargs):
        return self._deps.raw_payload._normalize_message_dict(*args, **kwargs)

    def _normalize_raw_media_fields(self, *args, **kwargs):
        return self._deps.raw_payload._normalize_raw_media_fields(*args, **kwargs)

    def _message_dict_has_content(self, *args, **kwargs):
        return self._deps.raw_payload._message_dict_has_content(*args, **kwargs)

    def _message_object_has_content(self, *args, **kwargs):
        return self._deps.raw_payload._message_object_has_content(*args, **kwargs)

    def _raw_attachment_types_from_message_dict(self, *args, **kwargs):
        return self._deps.raw_payload._raw_attachment_types_from_message_dict(*args, **kwargs)

    def _payload_message_dict(self, *args, **kwargs):
        return self._deps.raw_payload._payload_message_dict(*args, **kwargs)

    def _raw_payload_message_identity(self, *args, **kwargs):
        return self._deps.raw_payload._raw_payload_message_identity(*args, **kwargs)

    def _find_nested_message_dict(self, *args, **kwargs):
        return self._deps.raw_payload._find_nested_message_dict(*args, **kwargs)

    def _message_object_from_dict(self, *args, **kwargs):
        return self._deps.raw_payload._message_object_from_dict(*args, **kwargs)

    def _cache_raw_history_payload(self, *args, **kwargs):
        return self._deps.raw_payload._cache_raw_history_payload(*args, **kwargs)

    def _get_cached_raw_history_message(self, *args, **kwargs):
        return self._deps.raw_payload._get_cached_raw_history_message(*args, **kwargs)

    def _raw_history_message_dicts(self, *args, **kwargs):
        return self._deps.raw_payload._raw_history_message_dicts(*args, **kwargs)

    def _find_raw_history_message_dict(self, *args, **kwargs):
        return self._deps.raw_payload._find_raw_history_message_dict(*args, **kwargs)

    async def _fetch_raw_history_payload(self, *args, **kwargs):
        return await self._deps.raw_payload._fetch_raw_history_payload(*args, **kwargs)

    def _prepare_empty_recovery_candidate(self, *args, **kwargs):
        return self._deps.raw_payload._prepare_empty_recovery_candidate(*args, **kwargs)

    def _build_unwrapped_channel_message(self, *args, **kwargs):
        return self._deps.raw_payload._build_unwrapped_channel_message(*args, **kwargs)

    def _build_raw_regular_message(self, *args, **kwargs):
        return self._deps.raw_payload._build_raw_regular_message(*args, **kwargs)

    def _log_raw_message_missing_chat_id(self, *args, **kwargs):
        return self._deps.raw_payload._log_raw_message_missing_chat_id(*args, **kwargs)

    def _log_raw_empty_message(self, *args, **kwargs):
        return self._deps.raw_payload._log_raw_empty_message(*args, **kwargs)

    def _log_raw_unhandled_message_payload(self, *args, **kwargs):
        return self._deps.raw_payload._log_raw_unhandled_message_payload(*args, **kwargs)

    def _log_raw_auxiliary_event(self, *args, **kwargs):
        return self._deps.raw_payload._log_raw_auxiliary_event(*args, **kwargs)

    def _log_typed_empty_message(self, *args, **kwargs):
        return self._deps.raw_payload._log_typed_empty_message(*args, **kwargs)

    def _attachment_type_name(self, *args, **kwargs):
        return self._deps.media._attachment_type_name(*args, **kwargs)

    def _normalize_attachment_type(self, *args, **kwargs):
        return self._deps.media._normalize_attachment_type(*args, **kwargs)

    def _attachment_filename(self, *args, **kwargs):
        return self._deps.media._attachment_filename(*args, **kwargs)

    def _duration_seconds(self, *args, **kwargs):
        return self._deps.media._duration_seconds(*args, **kwargs)

    def _safe_attachment_field_names(self, *args, **kwargs):
        return self._deps.media._safe_attachment_field_names(*args, **kwargs)

    def _fix_filename_encoding(self, *args, **kwargs):
        return self._deps.media._fix_filename_encoding(*args, **kwargs)

    def _build_filename(self, *args, **kwargs):
        return self._deps.media._build_filename(*args, **kwargs)

    def _extract_video_url(self, *args, **kwargs):
        return self._deps.media._extract_video_url(*args, **kwargs)

    def _extract_audio_url(self, *args, **kwargs):
        return self._deps.media._extract_audio_url(*args, **kwargs)

    def _safe_payload_error_code(self, *args, **kwargs):
        return self._deps.media._safe_payload_error_code(*args, **kwargs)

    def _is_safe_field_name(self, *args, **kwargs):
        return self._deps.raw_payload._is_safe_field_name(*args, **kwargs)

    def _download_client_profile_for_url(self, *args, **kwargs):
        return self._deps.media._download_client_profile_for_url(*args, **kwargs)

    def _download_headers_for_url(self, *args, **kwargs):
        return self._deps.media._download_headers_for_url(*args, **kwargs)

    async def _download_from_url(self, *args, **kwargs):
        return await self._deps.media._download_from_url(*args, **kwargs)

    async def _download_file_by_id(self, *args, **kwargs):
        return await self._deps.media._download_file_by_id(*args, **kwargs)

    async def _download_video_by_id(self, *args, **kwargs):
        return await self._deps.media._download_video_by_id(*args, **kwargs)

    async def _download_attachment(self, *args, **kwargs):
        return await self._deps.media._download_attachment(*args, **kwargs)
