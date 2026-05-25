import time

from src.runtime.health.metrics import render_prometheus_textfile, write_prometheus_textfile
from src.runtime.health.state import HealthSnapshot, SubsystemState


def _snapshot() -> HealthSnapshot:
    now = int(time.time())
    return HealthSnapshot(
        overall_status="healthy",
        updated_at=now,
        supervisor_started_at=now - 60,
        last_healthy_at=now,
        worker_restart_count=2,
        subsystems={
            "runtime": SubsystemState(name="runtime", status="healthy"),
            "max_link": SubsystemState(name="max_link", status="degraded"),
            "tg_link": SubsystemState(name="tg_link", status="healthy"),
            "storage": SubsystemState(name="storage", status="healthy"),
            "scheduler": SubsystemState(name="scheduler", status="healthy"),
            "alerting": SubsystemState(name="alerting", status="recovering"),
        },
    )


def test_prometheus_textfile_renderer_uses_health_and_queue_state():
    content = render_prometheus_textfile(
        _snapshot(),
        pending_inbound={"pending_count": 1, "oldest_created_at": 100},
        pending_outbound={"pending_count": 2, "oldest_created_at": 200},
        pending_media={"pending_count": 3, "oldest_created_at": 300},
        delivery_counts={"inbound_delivered": 5, "outbound_failed": 1},
        alert_outbox_size=4,
    )

    assert "maxtg_bridge_worker_restarts_total 2" in content
    assert 'maxtg_bridge_subsystem_status{status="degraded",subsystem="max_link"} 1' in content
    assert 'maxtg_bridge_pending_queue_messages{queue="inbound"} 1' in content
    assert 'maxtg_bridge_pending_queue_oldest_timestamp_seconds{queue="media"} 300' in content
    assert 'maxtg_bridge_delivery_total{direction="outbound",status="failed"} 1' in content
    assert "maxtg_bridge_alert_outbox_messages 4" in content


def test_prometheus_textfile_writer_is_atomic_and_can_be_disabled(tmp_path):
    target = tmp_path / "maxtg_bridge.prom"

    assert write_prometheus_textfile(None, "ignored") is False
    assert write_prometheus_textfile(target, "first\n") is True
    assert target.read_text(encoding="utf-8") == "first\n"

    assert write_prometheus_textfile(target, "second\n") is True
    assert target.read_text(encoding="utf-8") == "second\n"
    assert not target.with_suffix(".prom.tmp").exists()
