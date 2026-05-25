"""Prometheus textfile metrics for runtime health and durable queues."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from ...db.repository import Repository
from .state import HealthSnapshot, SUBSYSTEM_ORDER
from .store import RuntimeHealthStore


def _label_value(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{text}"'


def _labels(**labels: object) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f"{key}={_label_value(value)}" for key, value in sorted(labels.items())) + "}"


def _metric(name: str, value: int | float | None, **labels: object) -> str:
    numeric = 0 if value is None else value
    return f"{name}{_labels(**labels)} {numeric}"


def render_prometheus_textfile(
    snapshot: HealthSnapshot,
    *,
    pending_inbound: Optional[dict[str, Optional[int]]] = None,
    pending_outbound: Optional[dict[str, Optional[int]]] = None,
    pending_media: Optional[dict[str, Optional[int]]] = None,
    delivery_counts: Optional[dict[str, int]] = None,
    alert_outbox_size: int = 0,
) -> str:
    lines = [
        "# HELP maxtg_bridge_worker_restarts_total Bridge worker restart count.",
        "# TYPE maxtg_bridge_worker_restarts_total counter",
        _metric("maxtg_bridge_worker_restarts_total", snapshot.worker_restart_count),
        "# HELP maxtg_bridge_last_healthy_timestamp_seconds Last fully healthy timestamp.",
        "# TYPE maxtg_bridge_last_healthy_timestamp_seconds gauge",
        _metric("maxtg_bridge_last_healthy_timestamp_seconds", snapshot.last_healthy_at),
        "# HELP maxtg_bridge_subsystem_status Subsystem status as labelled gauge.",
        "# TYPE maxtg_bridge_subsystem_status gauge",
    ]

    statuses = ("starting", "healthy", "degraded", "recovering")
    for name in SUBSYSTEM_ORDER:
        state = snapshot.subsystems.get(name)
        active = state.status if state is not None else "starting"
        for status in statuses:
            lines.append(
                _metric(
                    "maxtg_bridge_subsystem_status",
                    1 if active == status else 0,
                    subsystem=name,
                    status=status,
                )
            )

    lines.extend(
        [
            "# HELP maxtg_bridge_pending_queue_messages Durable pending queue depth.",
            "# TYPE maxtg_bridge_pending_queue_messages gauge",
            _metric(
                "maxtg_bridge_pending_queue_messages",
                (pending_inbound or {}).get("pending_count"),
                queue="inbound",
            ),
            _metric(
                "maxtg_bridge_pending_queue_messages",
                (pending_outbound or {}).get("pending_count"),
                queue="outbound",
            ),
            _metric(
                "maxtg_bridge_pending_queue_messages",
                (pending_media or {}).get("pending_count"),
                queue="media",
            ),
            "# HELP maxtg_bridge_pending_queue_oldest_timestamp_seconds Oldest pending item timestamp.",
            "# TYPE maxtg_bridge_pending_queue_oldest_timestamp_seconds gauge",
            _metric(
                "maxtg_bridge_pending_queue_oldest_timestamp_seconds",
                (pending_inbound or {}).get("oldest_created_at"),
                queue="inbound",
            ),
            _metric(
                "maxtg_bridge_pending_queue_oldest_timestamp_seconds",
                (pending_outbound or {}).get("oldest_created_at"),
                queue="outbound",
            ),
            _metric(
                "maxtg_bridge_pending_queue_oldest_timestamp_seconds",
                (pending_media or {}).get("oldest_created_at"),
                queue="media",
            ),
            "# HELP maxtg_bridge_alert_outbox_messages Alert notification outbox depth.",
            "# TYPE maxtg_bridge_alert_outbox_messages gauge",
            _metric("maxtg_bridge_alert_outbox_messages", alert_outbox_size),
            "# HELP maxtg_bridge_delivery_total Delivery log totals by direction and status.",
            "# TYPE maxtg_bridge_delivery_total counter",
        ]
    )

    for key, value in sorted((delivery_counts or {}).items()):
        direction, _, status = key.partition("_")
        lines.append(
            _metric(
                "maxtg_bridge_delivery_total",
                value,
                direction=direction or "unknown",
                status=status or "unknown",
            )
        )

    return "\n".join(lines) + "\n"


def write_prometheus_textfile(path: str | Path | None, content: str) -> bool:
    if path is None:
        return False
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(target)
    return True


async def write_runtime_metrics_textfile(
    *,
    path: str | Path | None,
    health: RuntimeHealthStore | None,
    repo: Repository,
) -> bool:
    if path is None or health is None:
        return False

    snapshot = await health.get_snapshot()
    content = render_prometheus_textfile(
        snapshot,
        pending_inbound=await repo.count_pending_inbound(),
        pending_outbound=await repo.count_pending_outbound(),
        pending_media=await repo.count_pending_media(),
        delivery_counts=await repo.count_deliveries_since(0),
        alert_outbox_size=await health.outbox.size(),
    )
    return write_prometheus_textfile(path, content)


async def run_runtime_metrics_textfile(
    *,
    path: str | Path | None,
    health: RuntimeHealthStore | None,
    repo: Repository,
    interval_seconds: int = 30,
):
    if path is None or health is None:
        return
    interval = max(1, int(interval_seconds))
    while True:
        await write_runtime_metrics_textfile(path=path, health=health, repo=repo)
        await asyncio.sleep(interval)
