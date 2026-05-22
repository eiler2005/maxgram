from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

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
    client_session_factory: Callable[..., Any] = ClientSession
    handlers: list[MessageHandler] = field(default_factory=list)
    start_handlers: list[Callable] = field(default_factory=list)
    issue_handlers: list[IssueHandler] = field(default_factory=list)
    interactive_ping_failure_limit: int = 3
    connection: ConnectionState = field(default_factory=ConnectionState)
    outbound: OutboundState = field(default_factory=OutboundState)
    raw_history: RawHistoryState = field(default_factory=RawHistoryState)
    empty_recovery: EmptyRecoveryState = field(default_factory=EmptyRecoveryState)
