import asyncio
import json
import logging

import pytest

from src.runtime.health import (
    RuntimeHealthStore,
    Severity,
    build_operator_alert,
    heartbeat_is_fresh,
)
from src.runtime.supervisor import BridgeSupervisor, SupervisorConfig
from src.runtime.tasks import create_logged_task


@pytest.mark.asyncio
async def test_report_issue_deduplicates_same_signature(tmp_path):
    health = RuntimeHealthStore(tmp_path)

    first = await health.report_issue(
        "max_link",
        code="session_invalid",
        summary="MAX сессия недействительна",
        raw_cause="Invalid token",
        severity=Severity.CRITICAL,
        requires_reauth=True,
    )
    second = await health.report_issue(
        "max_link",
        code="session_invalid",
        summary="MAX сессия недействительна",
        raw_cause="Invalid token",
        severity=Severity.CRITICAL,
        requires_reauth=True,
    )

    assert first.notify is True
    assert second.notify is False


@pytest.mark.asyncio
async def test_mark_healthy_after_issue_returns_recovered_and_clears_issue(tmp_path):
    health = RuntimeHealthStore(tmp_path)
    await health.mark_healthy("runtime", summary="Worker running", notify=False)
    await health.report_issue(
        "runtime",
        code="worker_crashed",
        summary="Worker crashed",
        raw_cause="boom",
        severity=Severity.ERROR,
    )

    change = await health.mark_healthy("runtime", summary="Worker running again")
    snapshot = await health.get_snapshot()

    assert change.transition == "recovered"
    assert change.notify is True
    assert snapshot.subsystems["runtime"].issue is None
    assert snapshot.subsystems["runtime"].status == "healthy"
    assert "восстановлен" in build_operator_alert(change)


@pytest.mark.asyncio
async def test_health_snapshot_schema_version_and_mismatch_falls_back(tmp_path):
    health = RuntimeHealthStore(tmp_path)
    await health.mark_healthy("runtime", summary="Worker running", notify=False)

    raw = json.loads((tmp_path / "health_state.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1

    raw["schema_version"] = 999
    (tmp_path / "health_state.json").write_text(json.dumps(raw), encoding="utf-8")
    reloaded = RuntimeHealthStore(tmp_path)
    snapshot = await reloaded.get_snapshot()

    assert snapshot.schema_version == 1
    assert snapshot.overall_status == "starting"


@pytest.mark.asyncio
async def test_supervisor_restarts_worker_and_writes_heartbeat(tmp_path):
    health = RuntimeHealthStore(tmp_path, heartbeat_interval_seconds=1)
    attempts = 0
    recovered = asyncio.Event()
    blocker = asyncio.Event()
    notifications = []

    async def worker():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("telegram polling failed")
        recovered.set()
        await blocker.wait()

    async def notify(text: str) -> bool:
        notifications.append(text)
        return True

    supervisor = BridgeSupervisor(
        health_store=health,
        worker_factory=worker,
        notify=notify,
        config=SupervisorConfig(
            heartbeat_interval_seconds=1,
            worker_restart_backoff_seconds=1,
        ),
    )

    task = asyncio.create_task(supervisor.run())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=2.5)
        snapshot = await health.get_snapshot()
        assert attempts >= 2
        assert snapshot.worker_restart_count >= 1
        assert any("аварийно завершился" in text for text in notifications)
        assert heartbeat_is_fresh(health.heartbeat_path, 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_supervisor_stops_worker_without_crash_alert(tmp_path):
    health = RuntimeHealthStore(tmp_path, heartbeat_interval_seconds=1)
    worker_cancelled = asyncio.Event()
    stop_event = asyncio.Event()
    notifications = []

    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            worker_cancelled.set()

    async def notify(text: str) -> bool:
        notifications.append(text)
        return True

    supervisor = BridgeSupervisor(
        health_store=health,
        worker_factory=worker,
        notify=notify,
        config=SupervisorConfig(
            heartbeat_interval_seconds=1,
            worker_restart_backoff_seconds=1,
        ),
    )

    task = asyncio.create_task(supervisor.run(stop_event=stop_event))
    await asyncio.sleep(0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert worker_cancelled.is_set()
    assert notifications == []
    assert heartbeat_is_fresh(health.heartbeat_path, 5)


def test_supervisor_restart_delay_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr("src.runtime.supervisor.random.uniform", lambda _a, _b: 1.0)
    supervisor = BridgeSupervisor(
        health_store=RuntimeHealthStore(tmp_path),
        worker_factory=lambda: asyncio.sleep(0),
        config=SupervisorConfig(
            worker_restart_backoff_seconds=5,
            worker_restart_max_backoff_seconds=30,
        ),
    )

    assert supervisor._restart_delay(1) == 5
    assert supervisor._restart_delay(2) == 10
    assert supervisor._restart_delay(3) == 20
    assert supervisor._restart_delay(10) == 30


@pytest.mark.asyncio
async def test_logged_detached_task_reports_exception(caplog):
    async def boom():
        raise RuntimeError("task boom")

    with caplog.at_level("ERROR", logger="tests.detached"):
        task = create_logged_task(
            boom(),
            logger=logging.getLogger("tests.detached"),
            name="boom_task",
        )
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)

    assert "Detached task failed: boom_task" in caplog.text
    assert "task boom" in caplog.text
