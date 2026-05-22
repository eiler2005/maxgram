from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from . import errors as max_errors
from .deps import RuntimeDeps
from .types import OutboundFailureState, PendingOutboundAck
from ...bridge.contracts import MaxIssue
from ...logging_utils import log_event

logger = logging.getLogger("src.adapters.max_adapter")


class MaxRuntimeService:
    def __init__(self, deps: RuntimeDeps):
        self._deps = deps

    @property
    def _last_outbound_failure(self):
        return self._deps.outbound.last_outbound_failure

    @_last_outbound_failure.setter
    def _last_outbound_failure(self, value):
        self._deps.outbound.last_outbound_failure = value

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

    @property
    def _issue_handlers(self):
        return self._deps.issue_handlers

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
        return max_errors.classify_runtime_error(error)

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
        return max_errors.is_retryable_send_error(error)

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
