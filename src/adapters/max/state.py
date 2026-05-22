from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from aiohttp import ClientSession

from .context import MaxAdapterContext
from .types import OutboundFailureState, PendingOutboundAck
from ..max_session_store import MaxSessionStore
from ...bridge.contracts import IssueHandler, MaxIssue, MessageHandler


@dataclass
class ConnectionState:
    client: object | None = None
    started: bool = False
    own_id: Optional[str] = None
    last_start_error: Optional[str] = None
    last_issue: Optional[MaxIssue] = None
    last_issue_notification_signature: Optional[str] = None
    last_connected_at: Optional[int] = None


@dataclass
class OutboundState:
    pending_outbound_acks: list[PendingOutboundAck] = field(default_factory=list)
    expected_outbound_ids: dict[tuple[str, str], float] = field(default_factory=dict)
    last_outbound_failure: OutboundFailureState = field(
        default_factory=lambda: OutboundFailureState(error=None, attempts=0)
    )


@dataclass
class RawHistoryState:
    raw_unwrapped_message_ids: dict[tuple[str, str], float] = field(default_factory=dict)
    raw_processed_message_ids: dict[tuple[str, str], float] = field(default_factory=dict)
    raw_history_messages: dict[tuple[str, str], tuple[float, object]] = field(default_factory=dict)
    expected_raw_history_messages: dict[str, tuple[str, float]] = field(default_factory=dict)


@dataclass
class EmptyRecoveryState:
    pending_empty_recovery_tasks: dict[tuple[str, str], asyncio.Task] = field(default_factory=dict)
    pending_empty_recoveries: dict[str, dict[str, object]] = field(default_factory=dict)
    pending_empty_recovery_worker: Optional[asyncio.Task] = None
    history_sweep_diagnostic_log_until: dict[tuple[str, str, str], float] = field(default_factory=dict)


@dataclass
class MaxRuntimeState:
    phone: str
    data_dir: str
    session_name: str
    session_store: MaxSessionStore
    backend: object
    tmp_dir: Path
    context: MaxAdapterContext
    client_session_factory: type[ClientSession] = ClientSession
    handlers: list[MessageHandler] = field(default_factory=list)
    start_handlers: list[Callable] = field(default_factory=list)
    issue_handlers: list[IssueHandler] = field(default_factory=list)
    interactive_ping_failure_limit: int = 3
    connection: ConnectionState = field(default_factory=ConnectionState)
    outbound: OutboundState = field(default_factory=OutboundState)
    raw_history: RawHistoryState = field(default_factory=RawHistoryState)
    empty_recovery: EmptyRecoveryState = field(default_factory=EmptyRecoveryState)

    _ATTR_MAP = {
        "_phone": ("phone",),
        "_data_dir": ("data_dir",),
        "_session_name": ("session_name",),
        "_session_store": ("session_store",),
        "_backend": ("backend",),
        "_tmp_dir": ("tmp_dir",),
        "_context": ("context",),
        "_client_session_factory": ("client_session_factory",),
        "_handlers": ("handlers",),
        "_start_handlers": ("start_handlers",),
        "_issue_handlers": ("issue_handlers",),
        "_interactive_ping_failure_limit": ("interactive_ping_failure_limit",),
        "_client": ("connection", "client"),
        "_started": ("connection", "started"),
        "_own_id": ("connection", "own_id"),
        "_last_start_error": ("connection", "last_start_error"),
        "_last_issue": ("connection", "last_issue"),
        "_last_issue_notification_signature": (
            "connection",
            "last_issue_notification_signature",
        ),
        "_last_connected_at": ("connection", "last_connected_at"),
        "_pending_outbound_acks": ("outbound", "pending_outbound_acks"),
        "_expected_outbound_ids": ("outbound", "expected_outbound_ids"),
        "_last_outbound_failure": ("outbound", "last_outbound_failure"),
        "_raw_unwrapped_message_ids": ("raw_history", "raw_unwrapped_message_ids"),
        "_raw_processed_message_ids": ("raw_history", "raw_processed_message_ids"),
        "_raw_history_messages": ("raw_history", "raw_history_messages"),
        "_expected_raw_history_messages": (
            "raw_history",
            "expected_raw_history_messages",
        ),
        "_pending_empty_recovery_tasks": (
            "empty_recovery",
            "pending_empty_recovery_tasks",
        ),
        "_pending_empty_recoveries": ("empty_recovery", "pending_empty_recoveries"),
        "_pending_empty_recovery_worker": (
            "empty_recovery",
            "pending_empty_recovery_worker",
        ),
        "_history_sweep_diagnostic_log_until": (
            "empty_recovery",
            "history_sweep_diagnostic_log_until",
        ),
    }

    def has_attr(self, name: str) -> bool:
        return name in self._ATTR_MAP

    def get_attr(self, name: str):
        target = self._ATTR_MAP[name]
        value = self
        for part in target:
            value = getattr(value, part)
        return value

    def set_attr(self, name: str, value) -> bool:
        target = self._ATTR_MAP.get(name)
        if target is None:
            return False
        owner = self
        for part in target[:-1]:
            owner = getattr(owner, part)
        setattr(owner, target[-1], value)
        return True
