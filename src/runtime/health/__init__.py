"""Persistent runtime health model, events, heartbeat, and notification outbox."""

from .heartbeat import heartbeat_is_fresh
from .outbox import AlertOutboxStore, OutboxMessage
from .rendering import (
    build_operator_alert,
    format_timestamp,
    humanize_duration,
    render_health_summary,
    status_badge,
    subsystem_label,
)
from .state import (
    STATUS_BADGES,
    SUBSYSTEM_LABELS,
    SUBSYSTEM_ORDER,
    HealthChange,
    HealthIssue,
    HealthSnapshot,
    Severity,
    SubsystemState,
)
from .store import RuntimeHealthStore

__all__ = [
    "AlertOutboxStore",
    "HealthChange",
    "HealthIssue",
    "HealthSnapshot",
    "OutboxMessage",
    "RuntimeHealthStore",
    "STATUS_BADGES",
    "SUBSYSTEM_LABELS",
    "SUBSYSTEM_ORDER",
    "Severity",
    "SubsystemState",
    "build_operator_alert",
    "format_timestamp",
    "heartbeat_is_fresh",
    "humanize_duration",
    "render_health_summary",
    "status_badge",
    "subsystem_label",
]
