import json

from aiohttp.test_utils import TestClient, TestServer

from src.config.loader import StatusApiConfig
from src.runtime.health.state import Severity
from src.runtime.health.store import RuntimeHealthStore
from src.runtime.status_api import (
    STATUS_API_SCHEMA_VERSION,
    build_status_app,
    build_status_payload,
)

RAW_CAUSE = "ConnectionResetError: SECRET-RAW-CAUSE-MUST-NOT-LEAK"
TOKEN = "test-status-token"


class FakeRepo:
    async def count_pending_inbound(self):
        return {"pending_count": 1, "oldest_created_at": 100}

    async def count_pending_outbound(self):
        return {"pending_count": 2, "oldest_created_at": 200}

    async def count_pending_media(self):
        return {"pending_count": 3, "oldest_created_at": 300}

    async def count_deliveries_since(self, _since):
        return {"inbound_delivered": 5, "outbound_failed": 1}


async def _store_with_issue(tmp_path) -> RuntimeHealthStore:
    store = RuntimeHealthStore(tmp_path)
    await store.report_issue(
        "max_link",
        code="max_egress_unavailable",
        summary="MAX egress недоступен",
        raw_cause=RAW_CAUSE,
        severity=Severity.CRITICAL,
        requires_reauth=True,
        notify=False,
    )
    return store


def _config(**overrides) -> StatusApiConfig:
    return StatusApiConfig(enabled=True, host="127.0.0.1", port=0, token=TOKEN, **overrides)


async def _client(store: RuntimeHealthStore) -> TestClient:
    app = build_status_app(
        config=_config(),
        health=store,
        repo=FakeRepo(),
        heartbeat_interval_seconds=30,
        egress_active="home_ru_proxy",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_status_payload_exposes_health_and_queue_state(tmp_path):
    store = await _store_with_issue(tmp_path)
    await store.write_heartbeat()

    payload = await build_status_payload(
        health=store,
        repo=FakeRepo(),
        egress_active="home_ru_proxy",
    )

    assert payload["schema_version"] == STATUS_API_SCHEMA_VERSION
    assert payload["overall_status"] == "degraded"
    assert payload["max_egress_active"] == "home_ru_proxy"
    assert payload["queues"]["inbound"]["pending_count"] == 1
    assert payload["queues"]["media"]["oldest_created_at"] == 300
    assert payload["delivery_totals"]["outbound_failed"] == 1
    assert payload["alert_outbox_size"] == 0
    assert payload["heartbeat_age_seconds"] is not None

    max_link = next(s for s in payload["subsystems"] if s["name"] == "max_link")
    assert max_link["status"] == "degraded"
    assert max_link["issue"]["code"] == "max_egress_unavailable"
    assert max_link["issue"]["severity"] == "critical"
    assert max_link["issue"]["requires_reauth"] is True


async def test_status_payload_never_carries_raw_cause(tmp_path):
    """Privacy: наружу уходят коды и статусы, но не текст исключений."""
    store = await _store_with_issue(tmp_path)

    payload = await build_status_payload(health=store, repo=FakeRepo())
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "SECRET-RAW-CAUSE" not in serialized
    assert "raw_cause" not in serialized
    assert TOKEN not in serialized


async def test_healthz_reports_heartbeat_freshness(tmp_path):
    store = RuntimeHealthStore(tmp_path)
    client = await _client(store)
    try:
        stale = await client.get("/healthz")
        assert stale.status == 503
        assert (await stale.json())["status"] == "stale"

        await store.write_heartbeat()
        fresh = await client.get("/healthz")
        assert fresh.status == 200
        assert (await fresh.json())["status"] == "ok"
    finally:
        await client.close()


async def test_status_endpoint_requires_bearer_token(tmp_path):
    store = await _store_with_issue(tmp_path)
    client = await _client(store)
    try:
        assert (await client.get("/status")).status == 401
        assert (await client.get("/status", headers={"Authorization": "Bearer wrong"})).status == 401
        assert (await client.get("/status", headers={"Authorization": TOKEN})).status == 401

        ok = await client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status == 200
        body = await ok.json()
        assert body["overall_status"] == "degraded"
    finally:
        await client.close()
