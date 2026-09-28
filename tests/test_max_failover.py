"""Fault injection for policy, connection generations and operator trace delivery."""
import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.adapters.max.adapter import MaxAdapter
from src.adapters.max.network.switching import OperationGate, EgressOperationInterrupted, GuardedClientPort
from src.adapters.max.backends.pymax.client_factory import create_pymax_client
from src.adapters.max.network import build_max_egress_profile
from src.adapters.tg.notifier import TelegramNotifier
from src.bridge.outbound_retry import is_definite_unsent_outbound_error
from src.config.loader import _load_max_egress
from src.runtime.health import AlertOutboxStore
from src.runtime.max_egress import MaxEgressController
from src.watchdog_external.config import WatchdogConfig
from src.watchdog_external.rules import _evaluate_status
from tests.fakes.fake_max_backend import FakeMaxBackend, FakeMaxClient


def config(**overrides):
    return _load_max_egress({"egress": {
        "active": "home_ru_proxy", "fallback_policy": "auto",
        "profiles": {"home_ru_proxy": {"type": "http_connect", "proxy_url": "http://user:secret@proxy.example.invalid:8080"}},
        **overrides,
    }})


class Clock:
    now = 1000
    def __call__(self):
        return self.now


class Adapter:
    def __init__(self):
        self.active = "home_ru_proxy"
        self.ready = False
        self.issue = None
        self.routes = {"home_ru_proxy": False, "hetzner_direct": True}
        self.connects = {"home_ru_proxy": True, "hetzner_direct": True}
        self.switches = []

    def is_ready(self):
        return self.ready

    def get_last_issue(self):
        return self.issue

    def get_egress_status(self):
        return {"max_egress_active": self.active}

    async def probe_egress(self, name):
        return {"ok": self.routes[name], "stage": "target_tls", "error": "SECRET", "target_host": "private"}

    async def switch_egress(self, name, **kwargs):
        self.switches.append(name)
        self.active = name
        self.ready = self.connects[name]


@pytest.fixture
def rig(tmp_path):
    clock, adapter, notify, restart = Clock(), Adapter(), AsyncMock(), Mock()
    controller = MaxEgressController(adapter=adapter, config=config(),
        path=tmp_path / "state.json", notify=notify, restart=restart, clock=clock)
    async def wait_ready():
        return adapter.ready
    controller._wait_ready = wait_ready
    return controller, adapter, clock, notify, restart


async def advance(rig, seconds):
    controller, _, clock, *_ = rig
    for _ in range(seconds // 30):
        clock.now += 30
        await controller.tick()


async def test_short_router_reboot_never_switches(rig):
    c, a, clock, notify, _ = rig
    await c.tick()
    await advance(rig, 300)
    assert not a.switches
    assert notify.await_count == 1
    a.routes[c.primary] = a.ready = True
    await advance(rig, 30)
    assert not a.switches
    assert c.state["failed_since"] is None
    assert notify.call_args.kwargs["audience"] == "ops"


async def test_failover_return_and_trace(rig):
    c, a, clock, notify, _ = rig
    await c.tick()
    await advance(rig, 570)
    assert not a.switches
    await advance(rig, 30)
    assert a.switches == [c.fallback]
    assert c.snapshot()["phase"] == "fallback"
    assert notify.call_args.kwargs["audience"] == "both"
    a.routes[c.primary] = True
    await advance(rig, 300)
    assert len(a.switches) == 1  # minimum residence is ten minutes
    await advance(rig, 300)
    assert a.switches == [c.fallback, c.primary]
    assert c.state["phase"] == "primary"
    assert "SECRET" not in json.dumps(c.state)
    assert "private" not in json.dumps(c.snapshot())
    ids = [call.kwargs["event_id"] for call in notify.call_args_list]
    assert len(ids) == len(set(ids))


async def test_failure_timer_reset_and_monitoring_gap(rig):
    c, a, clock, *_ = rig
    await c.tick()
    await advance(rig, 570)
    a.routes[c.primary] = True
    await advance(rig, 30)
    a.routes[c.primary] = False
    await advance(rig, 30)
    await advance(rig, 570)
    assert not a.switches
    clock.now += 3600
    await c.tick()
    assert not a.switches
    assert c.state["failed_since"] == clock.now


async def test_both_paths_down_and_cooldown(rig):
    c, a, _, notify, _ = rig
    a.routes[c.fallback] = False
    await c.tick()
    await advance(rig, 600)
    assert not a.switches
    assert "Оба маршрута" in notify.call_args.args[0]
    count = notify.await_count
    await advance(rig, 300)
    assert notify.await_count == count


@pytest.mark.parametrize("ready,reauth", [(True, False), (False, True)])
async def test_online_or_auth_failure_never_switches(rig, ready, reauth):
    c, a, *_ = rig
    a.ready = ready
    a.issue = SimpleNamespace(requires_reauth=reauth)
    await c.tick()
    await advance(rig, 900)
    assert not a.switches


async def test_failed_login_rolls_back_and_limits_retries(rig):
    c, a, clock, *_ = rig
    a.connects[c.fallback] = False
    await c.tick()
    await advance(rig, 600)
    assert a.switches == [c.fallback, c.primary]
    assert c.state["cooldown_until"] == clock.now + 600
    a.ready = False
    await advance(rig, 570)
    assert len(a.switches) == 2


async def test_failed_return_stays_on_fallback(rig):
    c, a, clock, *_ = rig
    await c.tick()
    await advance(rig, 600)
    a.routes[c.primary] = True
    a.connects[c.primary] = False
    await advance(rig, 600)
    assert a.switches == [c.fallback, c.primary, c.fallback]
    assert a.ready
    assert c.state["active"] == c.fallback


@pytest.mark.parametrize("phase", ["primary", "waiting", "switching", "fallback", "returning", "rollback"])
async def test_restart_revalidates_without_losing_cooldown(rig, phase):
    c, a, clock, notify, restart = rig
    c.state.update(phase=phase, active=c.fallback, cooldown_until=clock.now + 600,
                   fallback_since=clock.now - 30, healthy_since=clock.now - 500)
    c._save()
    replacement = MaxEgressController(adapter=a, config=c.cfg, path=c.path,
                                      notify=notify, restart=restart, clock=clock)
    await replacement.tick()
    assert a.switches == [c.fallback]
    assert replacement.state["healthy_since"] is None
    assert replacement.state["cooldown_until"] >= clock.now + 600


async def test_close_failure_requests_process_restart(rig):
    c, a, _, _, restart = rig
    a.switch_egress = AsyncMock(side_effect=TimeoutError)
    await c.tick()
    await advance(rig, 600)
    restart.assert_called_once_with("max_egress_close_failed")


async def test_gate_drains_and_rejects_old_generation():
    gate = OperationGate()
    entered = asyncio.Event()
    async def operation():
        entered.set()
        await asyncio.Event().wait()
    port = GuardedClientPort(SimpleNamespace(raw_request=operation), gate)
    task = asyncio.create_task(port.raw_request())
    await entered.wait()
    await gate.drain(timeout=0.001)
    with pytest.raises(EgressOperationInterrupted) as error:
        await task
    assert not is_definite_unsent_outbound_error(str(error.value))
    gate.generation += 1
    gate.open.set()
    with pytest.raises(RuntimeError, match="generation expired"):
        await port.raw_request()


async def test_gate_admits_new_operation_only_after_resume():
    gate = OperationGate()
    await gate.drain()
    operation = AsyncMock(return_value=42)
    task = asyncio.create_task(gate.run(operation))
    await asyncio.sleep(0)
    operation.assert_not_called()
    gate.open.set()
    assert await task == 42


async def test_real_adapter_switch_has_single_client(tmp_path):
    class Backend(FakeMaxBackend):
        def __init__(self):
            super().__init__()
            self.clients = []
        def create_client(self):
            assert not any(c.is_connected for c in self.clients)
            self.client = FakeMaxClient()
            self.clients.append(self.client)
            return self.client
        def set_egress(self, profile):
            self.profile = profile
    backend = Backend()
    adapter = MaxAdapter("+10000000000", str(tmp_path), "session.db", str(tmp_path / "tmp"),
                         backend=backend, egress_config=config())
    task = asyncio.create_task(adapter.start())
    async def ready():
        for _ in range(100):
            if adapter.is_ready():
                return
            await asyncio.sleep(0.01)
        pytest.fail("adapter failed to become ready")
    try:
        await ready()
        first = backend.client
        for name in ["hetzner_direct", "home_ru_proxy"]:
            await adapter.switch_egress(name)
            await ready()
            assert adapter.get_egress_status()["max_egress_active"] == name
            assert adapter._media._downloader._egress.profile.name == name
        assert not first.is_connected
        assert len(backend.clients) == 3
        assert await adapter.send_message("101", "test")
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await adapter.close()


@pytest.mark.parametrize("field,value", [("fallback_policy", "typo"), ("fallback", "home_ru_proxy"),
    ("failover_after_seconds", 0), ("probe_interval_seconds", True), ("drain_timeout_seconds", -1)])
def test_config_rejects_invalid_policy(field, value):
    with pytest.raises(ValueError):
        config(**{field: value})


def test_uploads_and_tcp_share_profile(tmp_path):
    cfg = config()
    for name in [cfg.active, cfg.fallback]:
        client = create_pymax_client(phone="+10000000000", data_dir=str(tmp_path),
                                     session_name="session.db", egress=build_max_egress_profile(cfg, name))
        if name == cfg.active:
            assert client.extra_config.proxy == "http://user:secret@proxy.example.invalid:8080"
        else:
            assert client.extra_config.proxy is None
        assert client.extra_config.relogin is False


async def test_notifier_audiences_dedup_and_outage_order(tmp_path):
    delivered = []
    online = False
    async def send(text, chat, topic, label):
        if not online:
            return False, "unavailable"
        delivered.append((text, label))
        return True, ""
    outbox = AlertOutboxStore(tmp_path / "outbox.jsonl")
    notifier = TelegramNotifier(owner_id=1, forum_group_id=2, ops_topic_id=3,
                                outbox_store=outbox, send_system_message=send)
    await notifier.send_system_notification("waiting", audience="ops", event_id="max-egress-1-1")
    await notifier.send_system_notification("connected", audience="both", event_id="max-egress-1-2")
    assert await outbox.size() == 3
    online = True
    await notifier.flush_notification_outbox()
    await notifier.send_system_notification("connected", audience="both", event_id="max-egress-1-2")
    assert delivered == [("waiting", "ops_topic"), ("connected", "owner_dm"), ("connected", "ops_topic")]
    assert await outbox.size() == 0


def test_external_watchdog_does_not_hide_other_issues():
    status = {"overall_status": "degraded", "subsystems": [{"name": "max_link", "issue": {
        "code": "max_egress_managed", "severity": "warning"}}],
        "max_egress_controller": {"policy": "auto", "ready": True, "phase": "fallback", "last_probe_at": 1000}}
    cfg = WatchdogConfig()
    assert "subsystem_issue" not in _evaluate_status(status, cfg, 1010)
    status["subsystems"].append({"name": "storage", "issue": {"code": "broken", "severity": "critical"}})
    findings = _evaluate_status(status, cfg, 1010)
    assert "subsystem_issue" in findings and "overall_degraded" in findings
    assert "subsystem_issue" in _evaluate_status(status, cfg, 1200)


async def test_pending_event_survives_notifier_failure(rig):
    c, a, clock, notify, restart = rig
    notify.side_effect = ConnectionError
    await c.tick()
    await advance(rig, 30)
    assert len(c.state["pending_events"]) == 1
    event_id = c.state["pending_events"][0]["id"]
    notify.side_effect = None
    recovered = MaxEgressController(adapter=a, config=c.cfg, path=c.path,
                                    notify=notify, restart=restart, clock=clock)
    await recovered.tick()
    assert not recovered.state["pending_events"]
    assert notify.call_args.kwargs["event_id"] == event_id


async def test_actual_probe_uses_pymax_endpoint_and_trust(monkeypatch):
    from src.adapters.max.backends.pymax.backend import PymaxBackend
    from src.adapters.max.backends.pymax.client_factory import make_extra_config
    profile = Mock()
    backend = PymaxBackend(phone="+10000000000", data_dir="/tmp", session_name="unused")
    backend.probe_egress(profile)
    options = make_extra_config()
    assert profile.probe.call_args.kwargs["host"] == options.host
    assert profile.probe.call_args.kwargs["port"] == options.port
    assert profile.probe.call_args.kwargs["ssl_context"].verify_mode != 0


async def test_dynamic_status_api_reads_new_route(tmp_path):
    from src.runtime.health import RuntimeHealthStore
    from src.runtime.status_api import build_status_payload
    from tests.test_status_api import FakeRepo
    route = {"max_egress_active": "home_ru_proxy", "max_egress_proxy_host": "PRIVATE"}
    store = RuntimeHealthStore(tmp_path)
    first = await build_status_payload(health=store, repo=FakeRepo(), egress_status_provider=lambda: route)
    route["max_egress_active"] = "hetzner_direct"
    second = await build_status_payload(health=store, repo=FakeRepo(), egress_status_provider=lambda: route)
    assert first["max_egress_active"] == "home_ru_proxy"
    assert second["max_egress_active"] == "hetzner_direct"
    assert "PRIVATE" not in json.dumps(second)


@pytest.mark.parametrize("phase,ready,deadline,wait_until,expected", [
    ("waiting", False, None, 1600, False),
    ("waiting", False, None, 900, True),
    ("switching", False, 1100, None, False),
    ("switching", False, 900, None, True),
    ("fallback", True, None, None, False),
    ("fallback", False, None, None, True),
])
def test_observer_expected_and_overdue_transitions(phase, ready, deadline, wait_until, expected):
    status = {"overall_status": "degraded", "max_egress_active": "hetzner_direct",
        "subsystems": [{"name": "max_link", "issue": {"code": "max_egress_managed"}}],
        "max_egress_controller": {"policy": "auto", "primary": "home_ru_proxy",
            "fallback": "hetzner_direct", "phase": phase, "ready": ready,
            "deadline": deadline, "wait_until": wait_until, "last_probe_at": 1000}}
    findings = _evaluate_status(status, WatchdogConfig(), 1010)
    assert ("subsystem_issue" in findings) == expected
    assert ("egress_mode_unexpected" in findings) == expected


async def test_failed_cdn_transfer_can_retry_on_new_route(tmp_path):
    from src.adapters.max.media.downloader import MaxCdnDownloader
    from src.adapters.max.network.switching import EgressSelection
    from contextlib import asynccontextmanager
    seen = []
    @asynccontextmanager
    async def response():
        yield SimpleNamespace(status=200, headers={"Content-Type": "image/jpeg"},
                              read=AsyncMock(return_value=b"\xff\xd8\xffimage"),
                              raise_for_status=lambda: None)
    @asynccontextmanager
    async def factory(**kwargs):
        seen.append(kwargs)
        yield SimpleNamespace(get=lambda url: response())
    cfg = config()
    selection = EgressSelection(build_max_egress_profile(cfg))
    downloader = MaxCdnDownloader(tmp_dir=tmp_path, client_session_factory=factory, egress=selection)
    downloader.operation_gate = OperationGate()
    path, _ = await downloader.download_from_url("https://cdn.example.invalid/image.jpg", "one", expected_kind="photo")
    assert path and seen[-1]["proxy"]
    selection.profile = build_max_egress_profile(cfg, cfg.fallback)
    path, _ = await downloader.download_from_url("https://cdn.example.invalid/image.jpg", "two", expected_kind="photo")
    assert path and "proxy" not in seen[-1]


async def test_probe_exception_is_safe_and_not_auth(rig):
    c, a, *_ = rig
    a.probe_egress = AsyncMock(side_effect=OSError("PRIVATE"))
    await c.tick()
    await advance(rig, 600)
    assert not a.switches
    assert "PRIVATE" not in c.path.read_text()


async def test_restored_fallback_does_not_connect_primary(tmp_path):
    backend = FakeMaxBackend()
    adapter = MaxAdapter("+10000000000", str(tmp_path), "session.db", str(tmp_path / "tmp"),
                         backend=backend, egress_config=config())
    adapter.restore_egress("hetzner_direct")
    assert backend.egress.name == "hetzner_direct"
    assert adapter._state.connection.client is None


async def test_fallback_reminder_and_flapping_recovery(rig):
    c, a, clock, notify, _ = rig
    await c.tick()
    await advance(rig, 600)
    a.routes[c.primary] = True
    await advance(rig, 270)
    a.routes[c.primary] = False
    await advance(rig, 30)
    assert c.state["healthy_since"] is None
    await advance(rig, 4 * 3600)
    assert a.switches == [c.fallback]
    assert any("продолжает использовать" in call.args[0] for call in notify.call_args_list)


async def test_switch_close_timeout_does_not_create_another_client(tmp_path, monkeypatch):
    adapter = MaxAdapter("+10000000000", str(tmp_path), "session.db", str(tmp_path / "tmp"),
                         backend=FakeMaxBackend(), egress_config=config())
    close = AsyncMock(side_effect=lambda: None)
    async def hang():
        await asyncio.Event().wait()
    adapter._state.connection.client = SimpleNamespace(close=hang)
    original_wait = asyncio.wait
    async def fast_wait(tasks, timeout=None):
        return await original_wait(tasks, timeout=0.01)
    monkeypatch.setattr(asyncio, "wait", fast_wait)
    with pytest.raises(TimeoutError, match="did not close"):
        await adapter.switch_egress("hetzner_direct")
    assert adapter._egress.name == "home_ru_proxy"
    assert not adapter._resume.is_set()
    assert not adapter._operation_gate.open.is_set()
    await asyncio.sleep(0)


async def test_watchdog_does_not_run_old_self_heal_during_managed_wait(rig, monkeypatch):
    from src.bridge.background import run_max_watchdog
    c, a, clock, *_ = rig
    loops = 0
    async def sleep(_):
        nonlocal loops
        loops += 1
        if loops > 3:
            raise asyncio.CancelledError
        clock.now += 30
    monkeypatch.setattr(asyncio, "sleep", sleep)
    restart = Mock()
    with pytest.raises(asyncio.CancelledError):
        await run_max_watchdog(max_adapter=a, health=None, send_ops_notification=AsyncMock(),
            emit_health_alert=AsyncMock(), egress_controller=c, restart_process=restart,
            self_heal_grace_seconds=0)
    assert not a.switches
    restart.assert_not_called()
