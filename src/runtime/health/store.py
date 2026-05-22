"""Persistent runtime health store."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

from .events import append_event
from .heartbeat import heartbeat_payload
from .outbox import AlertOutboxStore
from .state import (
    SUBSYSTEM_ORDER,
    HealthChange,
    HealthIssue,
    HealthSnapshot,
    Severity,
    SubsystemState,
    _now_ts,
)
from .writer import read_json, write_json_atomic


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
                return HealthSnapshot.from_dict(read_json(self._state_path))
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
            self._write_json_atomic(self._heartbeat_path, heartbeat_payload(self._snapshot))

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
        append_event(self._events_path, payload)

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]):
        write_json_atomic(path, payload)
