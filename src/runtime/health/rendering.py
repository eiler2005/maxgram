"""Operator-facing runtime health rendering."""

import time
from typing import Optional

from .state import (
    STATUS_BADGES,
    SUBSYSTEM_LABELS,
    SUBSYSTEM_ORDER,
    HealthChange,
    HealthSnapshot,
    Severity,
    _now_ts,
)


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
