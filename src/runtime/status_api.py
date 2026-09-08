"""
Read-only status API поверх runtime health.

Назначение — сделать наблюдаемыми изнутри известные, но наружу не выходящие
состояния (классы отказов F4, F5, F6, F15, F16 из docs/runbooks/watchdog.md).
Слушает только loopback: наружу отдаётся через SSH-канал внешнего watchdog,
публичного порта bridge по-прежнему не открывает.

Privacy: payload собирается только из health snapshot и durable-счётчиков.
Ни текста сообщений, ни названий чатов, ни телефонов, ни invite-ссылок,
ни `raw_cause` исключений сюда не попадает.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Optional

from aiohttp import web

from ..config.loader import StatusApiConfig
from ..db.repository import Repository
from ..logging_utils import log_event
from .health.heartbeat import heartbeat_is_fresh
from .health.metrics import collect_runtime_counters
from .health.state import SUBSYSTEM_ORDER, HealthSnapshot, _now_ts
from .health.store import RuntimeHealthStore

logger = logging.getLogger(__name__)

STATUS_API_SCHEMA_VERSION = 1

#: heartbeat считается протухшим после стольких интервалов записи
HEARTBEAT_STALE_INTERVALS = 3


def _heartbeat_age_seconds(health: RuntimeHealthStore) -> Optional[int]:
    path = health.heartbeat_path
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = int(raw.get("ts", 0))
    except Exception:
        return None
    if ts <= 0:
        return None
    return max(0, _now_ts() - ts)


def _subsystem_payload(snapshot: HealthSnapshot) -> list[dict[str, Any]]:
    names = list(SUBSYSTEM_ORDER) + [
        name for name in snapshot.subsystems if name not in SUBSYSTEM_ORDER
    ]
    payload: list[dict[str, Any]] = []
    for name in names:
        state = snapshot.subsystems.get(name)
        if state is None:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "status": state.status,
            "summary": state.summary,
            "updated_at": state.updated_at,
            "last_success_at": state.last_success_at,
            "issue": None,
        }
        if state.issue is not None:
            # raw_cause намеренно не отдаём: это текст исключения, а не ops-факт.
            entry["issue"] = {
                "code": state.issue.code,
                "summary": state.issue.summary,
                "severity": state.issue.severity.value,
                "requires_reauth": state.issue.requires_reauth,
                "first_seen_at": state.issue.first_seen_at,
                "last_seen_at": state.issue.last_seen_at,
            }
        payload.append(entry)
    return payload


async def build_status_payload(
    *,
    health: RuntimeHealthStore,
    repo: Repository,
    egress_active: str = "",
) -> dict[str, Any]:
    snapshot = await health.get_snapshot()
    counters = await collect_runtime_counters(health=health, repo=repo)
    now = _now_ts()

    return {
        "schema_version": STATUS_API_SCHEMA_VERSION,
        "generated_at": now,
        "overall_status": snapshot.overall_status,
        "updated_at": snapshot.updated_at,
        "last_healthy_at": snapshot.last_healthy_at,
        "worker_restart_count": snapshot.worker_restart_count,
        "uptime_seconds": max(0, now - snapshot.supervisor_started_at),
        "heartbeat_age_seconds": _heartbeat_age_seconds(health),
        "subsystems": _subsystem_payload(snapshot),
        "queues": {
            "inbound": counters["pending_inbound"],
            "outbound": counters["pending_outbound"],
            "media": counters["pending_media"],
        },
        "alert_outbox_size": counters["alert_outbox_size"],
        "delivery_totals": counters["delivery_counts"],
        "max_egress_active": egress_active,
    }


def _token_matches(expected: str, header: Optional[str]) -> bool:
    if not header:
        return False
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(expected, presented.strip())


def build_status_app(
    *,
    config: StatusApiConfig,
    health: RuntimeHealthStore,
    repo: Repository,
    heartbeat_interval_seconds: int = 30,
    egress_active: str = "",
) -> web.Application:
    max_age = max(5, int(heartbeat_interval_seconds) * HEARTBEAT_STALE_INTERVALS)

    async def handle_healthz(_request: web.Request) -> web.Response:
        fresh = heartbeat_is_fresh(health.heartbeat_path, max_age)
        return web.json_response(
            {
                "status": "ok" if fresh else "stale",
                "heartbeat_age_seconds": _heartbeat_age_seconds(health),
                "heartbeat_max_age_seconds": max_age,
            },
            status=200 if fresh else 503,
        )

    async def handle_status(request: web.Request) -> web.Response:
        if not _token_matches(config.token or "", request.headers.get("Authorization")):
            return web.json_response({"error": "unauthorized"}, status=401)
        payload = await build_status_payload(
            health=health,
            repo=repo,
            egress_active=egress_active,
        )
        return web.json_response(payload)

    app = web.Application()
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/status", handle_status)
    return app


async def run_status_api(
    *,
    config: StatusApiConfig,
    health: RuntimeHealthStore | None,
    repo: Repository,
    heartbeat_interval_seconds: int = 30,
    egress_active: str = "",
) -> None:
    """Держит loopback-сервер до отмены задачи.

    Никогда не роняет worker: любая ошибка старта логируется и задача завершается,
    внешний watchdog увидит это как `status_api_unreachable`.
    """
    if not config.enabled or health is None:
        return

    if not config.token:
        log_event(
            logger,
            logging.ERROR,
            "status_api.disabled",
            stage="status_api",
            outcome="skipped",
            reason="token_not_configured",
        )
        return

    app = build_status_app(
        config=config,
        health=health,
        repo=repo,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        egress_active=egress_active,
    )
    runner = web.AppRunner(app, access_log=None)

    try:
        await runner.setup()
        site = web.TCPSite(runner, config.host, int(config.port))
        await site.start()
    except Exception as e:
        log_event(
            logger,
            logging.ERROR,
            "status_api.start_failed",
            stage="status_api",
            outcome="failed",
            error=str(e),
        )
        await runner.cleanup()
        return

    log_event(
        logger,
        logging.INFO,
        "status_api.started",
        stage="status_api",
        outcome="listening",
        bind=f"{config.host}:{config.port}",
    )

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
