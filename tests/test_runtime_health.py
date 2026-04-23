import asyncio

import pytest

from src.runtime.health import (
    RuntimeHealthStore,
    Severity,
    build_operator_alert,
    heartbeat_is_fresh,
)
from src.runtime.supervisor import BridgeSupervisor, SupervisorConfig


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
