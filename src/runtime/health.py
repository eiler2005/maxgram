"""Persistent runtime health model, events, heartbeat, and notification outbox."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
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


@dataclass
class OutboxMessage:
    id: str
    text: str
    chat_id: int
    message_thread_id: Optional[int]
    label: str
    category: str
    created_at: int
    attempts: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "chat_id": self.chat_id,
            "message_thread_id": self.message_thread_id,
            "label": self.label,
            "category": self.category,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OutboxMessage":
        return cls(
            id=str(raw.get("id", uuid.uuid4().hex)),
            text=str(raw.get("text", "")),
            chat_id=int(raw["chat_id"]),
            message_thread_id=(
                int(raw["message_thread_id"]) if raw.get("message_thread_id") is not None else None
            ),
            label=str(raw.get("label", "unknown")),
            category=str(raw.get("category", "system")),
            created_at=int(raw.get("created_at", _now_ts())),
            attempts=int(raw.get("attempts", 0)),
            last_error=str(raw.get("last_error", "")),
        )


class AlertOutboxStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = asyncio.Lock()

    async def load(self) -> list[OutboxMessage]:
        async with self._lock:
            return self._load_unlocked()

    async def queue(self, message: OutboxMessage):
        async with self._lock:
            items = self._load_unlocked()
            items.append(message)
            self._rewrite_unlocked(items)

    async def rewrite(self, messages: list[OutboxMessage]):
        async with self._lock:
            self._rewrite_unlocked(messages)

    async def size(self) -> int:
        async with self._lock:
            return len(self._load_unlocked())

    def _load_unlocked(self) -> list[OutboxMessage]:
        if not self._path.exists():
            return []

        messages: list[OutboxMessage] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(OutboxMessage.from_dict(json.loads(line)))
                except Exception:
                    continue
        return messages

    def _rewrite_unlocked(self, messages: list[OutboxMessage]):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for message in messages:
                fh.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
        tmp_path.replace(self._path)


class RuntimeHealthStore:
    def __init__(self,
                 data_dir: str | Path,
                 *,
                 reminder_interval_hours: int = 4,
                 heartbeat_interval_seconds: int = 30):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._data_dir / "health_state.json"
        self._events_path = self._data_dir / "health_events.jsonl"
        self._heartbeat_path = self._data_dir / "health_heartbeat.json"
        self.reminder_interval_hours = reminder_interval_hours
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._lock = asyncio.Lock()
        self._snapshot = self._load_snapshot()
        self.outbox = AlertOutboxStore(self._data_dir / "alert_outbox.jsonl")

    @property
    def heartbeat_path(self) -> Path:
        return self._heartbeat_path

    def _load_snapshot(self) -> HealthSnapshot:
        if self._state_path.exists():
            try:
                raw = json.loads(self._state_path.read_text(encoding="utf-8"))
                return HealthSnapshot.from_dict(raw)
            except Exception:
                pass
        return self._build_default_snapshot()

    def _build_default_snapshot(self) -> HealthSnapshot:
        now = _now_ts()
        return HealthSnapshot(
            overall_status="starting",
            updated_at=now,
            supervisor_started_at=now,
            last_healthy_at=None,
            worker_restart_count=0,
            subsystems={
                name: SubsystemState(name=name)
                for name in SUBSYSTEM_ORDER
            },
        )

    async def get_snapshot(self) -> HealthSnapshot:
        async with self._lock:
            return deepcopy(self._snapshot)

    async def write_heartbeat(self):
        async with self._lock:
            payload = {
                "ts": _now_ts(),
                "overall_status": self._snapshot.overall_status,
                "worker_restart_count": self._snapshot.worker_restart_count,
            }
            self._write_json_atomic(self._heartbeat_path, payload)

    async def increment_worker_restarts(self):
        async with self._lock:
            self._snapshot.worker_restart_count += 1
            self._snapshot.updated_at = _now_ts()
            self._recompute_overall_status()
            self._persist_snapshot_unlocked()
            self._append_event_unlocked(
                {
                    "ts": self._snapshot.updated_at,
                    "event": "worker_restart_scheduled",
                    "worker_restart_count": self._snapshot.worker_restart_count,
                }
            )

    async def mark_recovering(self,
                              subsystem: str,
                              *,
                              summary: str,
                              notify: bool = False) -> HealthChange:
        async with self._lock:
            current_issue = deepcopy(self._ensure_state(subsystem).issue)
            return self._set_state_unlocked(
                subsystem,
                status="recovering",
                summary=summary,
                issue=current_issue,
                notify=notify,
            )

    async def mark_healthy(self,
                           subsystem: str,
                           *,
                           summary: str,
                           notify: bool = True) -> HealthChange:
        async with self._lock:
            state = self._ensure_state(subsystem)
            previous_state = deepcopy(state)
            previous_issue = deepcopy(state.issue)
            now = _now_ts()

            state.status = "healthy"
            state.summary = summary
            state.updated_at = now
            state.last_success_at = now
            state.last_transition = (
                "recovered"
                if (
                    previous_issue is not None
                    or (
                        previous_state.last_success_at is not None
                        and previous_state.status in {"degraded", "recovering"}
                    )
                )
                else "healthy"
            )
            state.issue = None

            self._snapshot.updated_at = now
            self._recompute_overall_status()
            self._persist_snapshot_unlocked()
            self._append_event_unlocked(
                {
                    "ts": now,
                    "event": "subsystem_transition",
                    "subsystem": subsystem,
                    "transition": state.last_transition,
                    "summary": summary,
                }
            )
            return HealthChange(
                subsystem=subsystem,
                transition=state.last_transition,
                notify=notify and state.last_transition == "recovered",
                state=deepcopy(state),
                snapshot=deepcopy(self._snapshot),
                previous_state=previous_state,
                previous_issue=previous_issue,
                issue=None,
            )

    async def report_issue(self,
                           subsystem: str,
                           *,
                           code: str,
                           summary: str,
                           raw_cause: str,
                           severity: Severity = Severity.ERROR,
                           impact: str = "",
                           operator_hint: str = "",
                           auto_recovery: str = "",
                           requires_reauth: bool = False,
                           notify: bool = True) -> HealthChange:
        async with self._lock:
            state = self._ensure_state(subsystem)
            previous_state = deepcopy(state)
            previous_issue = deepcopy(state.issue)
            now = _now_ts()

            issue = HealthIssue(
                code=code,
                summary=summary,
                raw_cause=raw_cause.strip(),
                severity=severity,
                impact=impact.strip(),
                operator_hint=operator_hint.strip(),
                auto_recovery=auto_recovery.strip(),
                requires_reauth=requires_reauth,
                first_seen_at=now,
                last_seen_at=now,
                last_success_at=state.last_success_at,
            )

            if previous_issue is not None and previous_issue.signature() == issue.signature():
                issue.first_seen_at = previous_issue.first_seen_at

            reason_changed = previous_issue is None or previous_issue.signature() != issue.signature()
            state.status = "degraded"
            state.summary = summary
            state.updated_at = now
            state.last_transition = "degraded"
            state.issue = issue

            self._snapshot.updated_at = now
            self._recompute_overall_status()
            self._persist_snapshot_unlocked()
            self._append_event_unlocked(
                {
                    "ts": now,
                    "event": "subsystem_issue",
                    "subsystem": subsystem,
                    "transition": "degraded",
                    "summary": summary,
                    "raw_cause": raw_cause.strip(),
                    "severity": severity.value,
                    "requires_reauth": requires_reauth,
                }
            )
            return HealthChange(
                subsystem=subsystem,
                transition="degraded",
                notify=notify and reason_changed,
                state=deepcopy(state),
                snapshot=deepcopy(self._snapshot),
                previous_state=previous_state,
                issue=deepcopy(issue),
                previous_issue=previous_issue,
                reason_changed=reason_changed,
            )

    async def set_supervisor_started(self):
        async with self._lock:
            now = _now_ts()
            self._snapshot.supervisor_started_at = now
            self._snapshot.updated_at = now
            self._persist_snapshot_unlocked()
            self._append_event_unlocked({"ts": now, "event": "supervisor_started"})

    async def append_event(self, payload: dict[str, Any]):
        async with self._lock:
            self._append_event_unlocked(payload)

    def _ensure_state(self, subsystem: str) -> SubsystemState:
        state = self._snapshot.subsystems.get(subsystem)
        if state is None:
            state = SubsystemState(name=subsystem)
            self._snapshot.subsystems[subsystem] = state
        return state

    def _set_state_unlocked(self,
                            subsystem: str,
                            *,
                            status: str,
                            summary: str,
                            issue: Optional[HealthIssue],
                            notify: bool) -> HealthChange:
        state = self._ensure_state(subsystem)
        previous_state = deepcopy(state)
        previous_issue = deepcopy(state.issue)
        now = _now_ts()

        state.status = status
        state.summary = summary
        state.updated_at = now
        state.last_transition = status
        state.issue = issue
        self._snapshot.updated_at = now
        self._recompute_overall_status()
        self._persist_snapshot_unlocked()
        self._append_event_unlocked(
            {
                "ts": now,
                "event": "subsystem_transition",
                "subsystem": subsystem,
                "transition": status,
                "summary": summary,
            }
        )
        return HealthChange(
            subsystem=subsystem,
            transition=status,
            notify=notify and previous_state.status != status,
            state=deepcopy(state),
            snapshot=deepcopy(self._snapshot),
            previous_state=previous_state,
            previous_issue=previous_issue,
            issue=deepcopy(issue),
        )

    def _recompute_overall_status(self):
        statuses = [state.status for state in self._snapshot.subsystems.values()]
        if any(status == "degraded" for status in statuses):
            overall = "degraded"
        elif any(status == "recovering" for status in statuses):
            overall = "recovering"
        elif all(status == "healthy" for status in statuses):
            overall = "healthy"
        else:
            overall = "starting"

        self._snapshot.overall_status = overall
        if overall == "healthy":
            self._snapshot.last_healthy_at = self._snapshot.updated_at

    def _persist_snapshot_unlocked(self):
        self._write_json_atomic(self._state_path, self._snapshot.to_dict())

    def _append_event_unlocked(self, payload: dict[str, Any]):
        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        with self._events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)


def format_timestamp(ts: Optional[int]) -> str:
    if ts is None:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def humanize_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    seconds = max(int(seconds), 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    if secs or not parts:
        parts.append(f"{secs}с")
    return " ".join(parts)


def subsystem_label(name: str) -> str:
    return SUBSYSTEM_LABELS.get(name, name)


def status_badge(status: str) -> str:
    return STATUS_BADGES.get(status, "•")


def render_health_summary(snapshot: HealthSnapshot) -> list[str]:
    lines = [
        "🩺 Runtime Health",
        f"  Общее состояние: {status_badge(snapshot.overall_status)} {snapshot.overall_status}",
        f"  Рестартов worker: {snapshot.worker_restart_count}",
    ]
    if snapshot.last_healthy_at:
        lines.append(f"  Последний fully healthy: {format_timestamp(snapshot.last_healthy_at)}")

    lines.append("")
    lines.append("🔎 Подсистемы")
    for name in SUBSYSTEM_ORDER:
        state = snapshot.subsystems.get(name)
        if state is None:
            continue
        lines.append(
            f"  {subsystem_label(name)}: {status_badge(state.status)} {state.status}"
        )
        if state.issue is not None and state.status != "healthy":
            lines.append(f"    {state.issue.summary}")
            if state.issue.requires_reauth:
                lines.append("    Требуется: reauth по SMS")
            if state.last_success_at:
                lines.append(f"    Последний healthy: {format_timestamp(state.last_success_at)}")
    return lines


def build_operator_alert(change: HealthChange) -> str:
    subsystem = subsystem_label(change.subsystem)
    state = change.state
    issue = change.issue or state.issue

    if change.transition == "recovered":
        previous_issue = change.previous_issue
        downtime = None
        if previous_issue is not None:
            downtime = max(0, _now_ts() - previous_issue.first_seen_at)
        lines = [f"✅ Bridge восстановлен: {subsystem}"]
        if previous_issue is not None:
            lines.append(f"Что восстановилось: {previous_issue.summary}")
            lines.append(f"Простой: ~{humanize_duration(downtime)}")
            if previous_issue.code == "link_offline":
                lines.append(
                    "Важно: сообщения, отправленные во время disconnect, могли не восстановиться автоматически."
                )
        lines.append(f"Текущее состояние: {state.summary}")
        if state.last_success_at:
            lines.append(f"Healthy с: {format_timestamp(state.last_success_at)}")
        return "\n".join(lines)

    if issue is None:
        return f"⚠️ Bridge degraded: {subsystem}"

    icon = "🚨" if issue.severity == Severity.CRITICAL else "⚠️"
    lines = [f"{icon} Bridge degraded: {subsystem}"]
    lines.append(f"Что сломано: {issue.summary}")
    if issue.impact:
        lines.append(f"Влияние: {issue.impact}")
    if issue.raw_cause:
        lines.append(f"Причина: {issue.raw_cause}")
    if issue.auto_recovery:
        lines.append(f"Система делает: {issue.auto_recovery}")
    if issue.operator_hint:
        lines.append(f"Нужно руками: {issue.operator_hint}")
    if issue.last_success_at:
        lines.append(f"Последний healthy: {format_timestamp(issue.last_success_at)}")
    return "\n".join(lines)


def heartbeat_is_fresh(path: str | Path, max_age_seconds: int) -> bool:
    heartbeat_path = Path(path)
    if not heartbeat_path.exists():
        return False

    try:
        raw = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        ts = int(raw.get("ts", 0))
    except Exception:
        return False

    return (_now_ts() - ts) <= max(1, int(max_age_seconds))
