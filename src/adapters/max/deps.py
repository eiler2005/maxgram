from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .backends.base import MaxBackend
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
    client_session_factory: Callable[..., Any]
    raw_payload: Any
    egress: Any | None = None


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
