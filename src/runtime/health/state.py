"""Runtime health domain state."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


def _now_ts() -> int:
    return int(time.time())


SUBSYSTEM_ORDER = (
    "runtime",
    "max_link",
    "tg_link",
    "storage",
    "scheduler",
    "alerting",
)

HEALTH_SNAPSHOT_SCHEMA_VERSION = 1

SUBSYSTEM_LABELS = {
    "runtime": "Runtime",
    "max_link": "MAX link",
    "tg_link": "Telegram link",
    "storage": "Storage",
    "scheduler": "Scheduler",
    "alerting": "Alerting",
}

STATUS_BADGES = {
    "starting": "⚪",
    "healthy": "✅",
    "degraded": "⚠️",
    "recovering": "🔄",
}


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class HealthIssue:
    code: str
    summary: str
    raw_cause: str
    severity: Severity = Severity.ERROR
    impact: str = ""
    operator_hint: str = ""
    auto_recovery: str = ""
    requires_reauth: bool = False
    first_seen_at: int = field(default_factory=_now_ts)
    last_seen_at: int = field(default_factory=_now_ts)
    last_success_at: Optional[int] = None

    def signature(self) -> str:
        return "|".join(
            [
                self.code,
                self.summary,
                self.raw_cause,
                self.severity.value,
                "reauth" if self.requires_reauth else "noreauth",
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "summary": self.summary,
            "raw_cause": self.raw_cause,
            "severity": self.severity.value,
            "impact": self.impact,
            "operator_hint": self.operator_hint,
            "auto_recovery": self.auto_recovery,
            "requires_reauth": self.requires_reauth,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "last_success_at": self.last_success_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HealthIssue":
        return cls(
            code=str(raw.get("code", "unknown")),
            summary=str(raw.get("summary", "")).strip() or "Unknown issue",
            raw_cause=str(raw.get("raw_cause", "")).strip(),
            severity=Severity(str(raw.get("severity", Severity.ERROR.value))),
            impact=str(raw.get("impact", "")).strip(),
            operator_hint=str(raw.get("operator_hint", "")).strip(),
            auto_recovery=str(raw.get("auto_recovery", "")).strip(),
            requires_reauth=bool(raw.get("requires_reauth", False)),
            first_seen_at=int(raw.get("first_seen_at", _now_ts())),
            last_seen_at=int(raw.get("last_seen_at", _now_ts())),
            last_success_at=(
                int(raw["last_success_at"]) if raw.get("last_success_at") is not None else None
            ),
        )


@dataclass
class SubsystemState:
    name: str
    status: str = "starting"
    summary: str = "ожидаем первый сигнал"
    updated_at: int = field(default_factory=_now_ts)
    last_success_at: Optional[int] = None
    last_transition: str = "starting"
    issue: Optional[HealthIssue] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "updated_at": self.updated_at,
            "last_success_at": self.last_success_at,
            "last_transition": self.last_transition,
            "issue": self.issue.to_dict() if self.issue is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], name: str) -> "SubsystemState":
        issue_raw = raw.get("issue")
        issue = HealthIssue.from_dict(issue_raw) if isinstance(issue_raw, dict) else None
        return cls(
            name=name,
            status=str(raw.get("status", "starting")),
            summary=str(raw.get("summary", "ожидаем первый сигнал")),
            updated_at=int(raw.get("updated_at", _now_ts())),
            last_success_at=(
                int(raw["last_success_at"]) if raw.get("last_success_at") is not None else None
            ),
            last_transition=str(raw.get("last_transition", raw.get("status", "starting"))),
            issue=issue,
        )


@dataclass
class HealthSnapshot:
    overall_status: str
    updated_at: int
    supervisor_started_at: int
    last_healthy_at: Optional[int]
    worker_restart_count: int
    subsystems: dict[str, SubsystemState]
    schema_version: int = HEALTH_SNAPSHOT_SCHEMA_VERSION

    def active_issues(self) -> list[tuple[str, SubsystemState, HealthIssue]]:
        issues: list[tuple[str, SubsystemState, HealthIssue]] = []
        for name in SUBSYSTEM_ORDER:
            state = self.subsystems.get(name)
            if state is None or state.issue is None or state.status == "healthy":
                continue
            issues.append((name, state, state.issue))
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "overall_status": self.overall_status,
            "updated_at": self.updated_at,
            "supervisor_started_at": self.supervisor_started_at,
            "last_healthy_at": self.last_healthy_at,
            "worker_restart_count": self.worker_restart_count,
            "subsystems": {
                name: state.to_dict()
                for name, state in self.subsystems.items()
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HealthSnapshot":
        schema_version = int(raw.get("schema_version", HEALTH_SNAPSHOT_SCHEMA_VERSION))
        if schema_version != HEALTH_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported health snapshot schema_version={schema_version}")
        subsystems_raw = raw.get("subsystems") or {}
        subsystems: dict[str, SubsystemState] = {}
        for name in SUBSYSTEM_ORDER:
            item = subsystems_raw.get(name, {})
            if not isinstance(item, dict):
                item = {}
            subsystems[name] = SubsystemState.from_dict(item, name=name)
        return cls(
            overall_status=str(raw.get("overall_status", "starting")),
            updated_at=int(raw.get("updated_at", _now_ts())),
            supervisor_started_at=int(raw.get("supervisor_started_at", _now_ts())),
            last_healthy_at=(
                int(raw["last_healthy_at"]) if raw.get("last_healthy_at") is not None else None
            ),
            worker_restart_count=int(raw.get("worker_restart_count", 0)),
            subsystems=subsystems,
            schema_version=schema_version,
        )


@dataclass
class HealthChange:
    subsystem: str
    transition: str
    notify: bool
    state: SubsystemState
    snapshot: HealthSnapshot
    previous_state: Optional[SubsystemState] = None
    issue: Optional[HealthIssue] = None
    previous_issue: Optional[HealthIssue] = None
    reason_changed: bool = False
