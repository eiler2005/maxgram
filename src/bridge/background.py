"""Bridge background loops."""

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Optional

from .contracts import (
    MAX_DM_SWEEP_BACKFILL_SECONDS,
    MaxBridgePort,
    TelegramBridgePort,
    is_probable_client_cid,
)
from ..config.loader import AppConfig
from ..db.repository import Repository
from ..logging_utils import build_max_flow_id, log_event
from ..runtime.health import RuntimeHealthStore, Severity

logger = logging.getLogger("src.bridge.core")


def _default_restart_process(reason: str) -> None:
    log_event(
        logger,
        logging.CRITICAL,
        "bridge.watchdog.process_exit",
        stage="watchdog",
        outcome="exiting",
        reason=reason,
    )
    os._exit(75)


def _egress_is_home_ru_proxy(max_adapter: MaxBridgePort) -> bool:
    status = max_adapter.get_egress_status()
    return bool(status and status.get("max_egress_active") == "home_ru_proxy")


def _probe_summary(probe: dict[str, object] | None) -> str:
    if not probe:
        return "probe result missing"
    stage = str(probe.get("stage") or "unknown")
    error = str(probe.get("error") or "").strip()
    if error:
        return f"{stage}: {error}"
    return stage


def _load_last_self_heal_restart(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = data.get("last_restart_at")
    return int(value) if isinstance(value, int) else None


def _persist_self_heal_restart(path: Path | None, *, reason: str, probe: dict[str, object] | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_restart_at": int(time.time()),
        "reason": reason,
        "probe_stage": (probe or {}).get("stage"),
        "probe_ok": bool((probe or {}).get("ok")),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _self_heal_restart_allowed(
    path: Path | None,
    *,
    cooldown_seconds: int,
    now: int | None = None,
) -> bool:
    last_restart_at = _load_last_self_heal_restart(path)
    if last_restart_at is None:
        return True
    return (now or int(time.time())) - last_restart_at >= cooldown_seconds


async def run_periodic_status(
    *,
    health: Optional[RuntimeHealthStore],
    build_status_message: Callable[[int], Awaitable[str]],
    send_ops_notification: Callable[[str], Awaitable[None]],
    interval_hours: int = 4,
):
    await asyncio.sleep(interval_hours * 3600)
    while True:
        try:
            text = await build_status_message(interval_hours)
            await send_ops_notification(text)
            if health is not None:
                await health.mark_healthy(
                    "scheduler",
                    summary="Планировщик периодических статус-отчётов работает",
                    notify=False,
                )
            logger.info("Periodic status sent")
        except Exception as e:
            logger.error("Periodic status error: %s", e)
            if health is not None:
                await health.report_issue(
                    "scheduler",
                    code="periodic_status_failed",
                    summary="Периодический 4h status не смог отправиться",
                    raw_cause=str(e),
                    severity=Severity.ERROR,
                    impact="Оператор может не получить очередной health reminder вовремя.",
                    operator_hint="Проверь Telegram notifier и состояние scheduler task.",
                    auto_recovery="Следующая попытка будет на следующем цикле scheduler.",
                    notify=False,
                )
        await asyncio.sleep(interval_hours * 3600)


async def run_max_watchdog(
    *,
    max_adapter: MaxBridgePort,
    health: Optional[RuntimeHealthStore],
    send_ops_notification: Callable[[str], Awaitable[None]],
    emit_health_alert: Callable[[object], Awaitable[None]],
    alert_after_seconds: int = 60,
    check_interval: int = 10,
    egress_probe_interval: int = 30,
    self_heal_grace_seconds: int = 180,
    self_heal_restart_cooldown_seconds: int = 1800,
    self_heal_state_path: Path | None = None,
    restart_process: Callable[[str], None] | None = None,
):
    disconnected_since: Optional[float] = None
    alert_sent = False
    last_egress_probe_at = 0.0
    restart = restart_process or _default_restart_process

    while True:
        await asyncio.sleep(check_interval)

        if max_adapter.is_ready():
            if alert_sent and health is None:
                downtime = int(time.time() - disconnected_since)
                await send_ops_notification(
                    f"⚠️ Возможен пропуск сообщений MAX за время простоя (~{downtime}с): "
                    "история во время disconnect не воспроизводится автоматически"
                )
                await send_ops_notification(
                    f"✅ MAX восстановлен (простой ~{downtime}с)"
                )
                log_event(
                    logger,
                    logging.INFO,
                    "bridge.watchdog.max_recovered",
                    stage="watchdog",
                    outcome="recovered",
                    downtime_seconds=downtime,
                )
            disconnected_since = None
            alert_sent = False
            last_egress_probe_at = 0.0
        else:
            if disconnected_since is None:
                disconnected_since = time.time()
                log_event(
                    logger,
                    logging.WARNING,
                    "bridge.watchdog.max_lost",
                    stage="watchdog",
                    outcome="started",
                )

            elapsed = time.time() - disconnected_since
            home_proxy_active = _egress_is_home_ru_proxy(max_adapter)
            latest_probe: dict[str, object] | None = None
            if home_proxy_active and (
                time.monotonic() - last_egress_probe_at >= max(0, egress_probe_interval)
            ):
                last_egress_probe_at = time.monotonic()
                try:
                    latest_probe = await max_adapter.probe_egress()
                except Exception as exc:
                    latest_probe = {
                        "ok": False,
                        "stage": "probe_call",
                        "error": str(exc).strip() or exc.__class__.__name__,
                    }
                    log_event(
                        logger,
                        logging.ERROR,
                        "bridge.watchdog.max_egress_probe_failed",
                        stage="watchdog",
                        outcome="failed",
                        error=latest_probe["error"],
                    )

                if latest_probe is not None and not latest_probe.get("ok") and health is not None:
                    await health.report_issue(
                        "max_link",
                        code="max_egress_unavailable",
                        summary=f"MAX egress home_ru_proxy недоступен: {_probe_summary(latest_probe)}",
                        raw_cause=_probe_summary(latest_probe),
                        severity=Severity.ERROR,
                        impact=(
                            "MAX не может подключиться через роутерный Channel M; входящие MAX "
                            "сообщения не поступают до восстановления egress."
                        ),
                        operator_hint=(
                            "Проверь роутерный reverse tunnel, sing-box ingress и VPS listener; "
                            "/status покажет последнюю egress probe."
                        ),
                        auto_recovery=(
                            "Watchdog продолжит попытки через home_ru_proxy; переключение на "
                            "hetzner_direct не выполняется автоматически."
                        ),
                        notify=False,
                    )

                if (
                    latest_probe is not None
                    and latest_probe.get("ok")
                    and elapsed >= self_heal_grace_seconds
                ):
                    if _self_heal_restart_allowed(
                        self_heal_state_path,
                        cooldown_seconds=self_heal_restart_cooldown_seconds,
                    ):
                        reason = (
                            "MAX stays offline after home_ru_proxy probe succeeded "
                            f"for {int(elapsed)}s"
                        )
                        _persist_self_heal_restart(
                            self_heal_state_path,
                            reason=reason,
                            probe=latest_probe,
                        )
                        log_event(
                            logger,
                            logging.CRITICAL,
                            "bridge.watchdog.max_self_heal_restart",
                            stage="watchdog",
                            outcome="scheduled",
                            downtime_seconds=int(elapsed),
                            probe_stage=latest_probe.get("stage"),
                            latency_ms=latest_probe.get("latency_ms"),
                            cooldown_seconds=self_heal_restart_cooldown_seconds,
                        )
                        if health is not None:
                            change = await health.report_issue(
                                "max_link",
                                code="max_self_heal_restart",
                                summary=(
                                    "MAX не выходит в online при рабочем home_ru_proxy — "
                                    "перезапускаю bridge процесс"
                                ),
                                raw_cause=reason,
                                severity=Severity.ERROR,
                                impact=(
                                    "Bridge будет кратко недоступен, затем Docker restart:always "
                                    "поднимет новый процесс."
                                ),
                                operator_hint=(
                                    "Если рестарты повторяются после cooldown, смотри MAX startup "
                                    "errors и Channel M probe в логах."
                                ),
                                auto_recovery="Процесс завершится сам, Docker пересоздаст bridge.",
                                notify=True,
                            )
                            await emit_health_alert(change)
                        else:
                            await send_ops_notification(
                                "⚠️ MAX не выходит в online при рабочем home_ru_proxy — "
                                "перезапускаю bridge процесс"
                            )
                        restart(reason)
                    else:
                        log_event(
                            logger,
                            logging.WARNING,
                            "bridge.watchdog.max_self_heal_restart_suppressed",
                            stage="watchdog",
                            outcome="rate_limited",
                            downtime_seconds=int(elapsed),
                            cooldown_seconds=self_heal_restart_cooldown_seconds,
                        )

            if not alert_sent and elapsed >= alert_after_seconds:
                log_event(
                    logger,
                    logging.ERROR,
                    "bridge.watchdog.max_alert",
                    stage="watchdog",
                    outcome="alerted",
                    downtime_seconds=int(elapsed),
                )
                if health is not None:
                    current_issue = max_adapter.get_last_issue()
                    if current_issue is None:
                        change = await health.report_issue(
                            "max_link",
                            code="link_offline",
                            summary=f"MAX недоступен уже {int(elapsed)}с — идёт переподключение",
                            raw_cause="MAX client is offline / reconnect loop active",
                            severity=Severity.ERROR,
                            impact=(
                                "Новые MAX сообщения не приходят, а история за время disconnect "
                                "не воспроизводится автоматически."
                            ),
                            operator_hint=(
                                "Если reconnect затянулся, проверь /status и при необходимости сделай "
                                "reauth по SMS."
                            ),
                            auto_recovery="MAX reconnect loop уже запущен и продолжит попытки автоматически.",
                            notify=True,
                        )
                        await emit_health_alert(change)
                else:
                    await send_ops_notification(
                        f"⚠️ MAX недоступен уже {int(elapsed)}с — идёт переподключение"
                    )
                alert_sent = True


async def run_dm_history_sweep(
    *,
    repo: Repository,
    max_adapter: MaxBridgePort,
    poll_interval: int = 120,
    limit: int = 30,
    backfill_seconds: int = MAX_DM_SWEEP_BACKFILL_SECONDS,
):
    log_event(
        logger,
        logging.INFO,
        "bridge.dm_history_sweep.worker_started",
        stage="history_sweep",
        outcome="started",
        poll_interval_seconds=poll_interval,
        limit=limit,
        backfill_seconds=backfill_seconds,
    )
    while True:
        try:
            since_ts = int(time.time()) - int(backfill_seconds)
            bindings = await repo.list_bindings()
            for binding in bindings:
                chat_id = str(binding.max_chat_id)
                if binding.mode != "active":
                    continue
                if chat_id.startswith("-") or is_probable_client_cid(chat_id):
                    continue
                flow_id = build_max_flow_id(chat_id, "history-sweep")
                await max_adapter.replay_recent_history(
                    chat_id,
                    limit=limit,
                    since_ts=since_ts,
                    flow_id=flow_id,
                )
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "bridge.dm_history_sweep.worker_failed",
                stage="history_sweep",
                outcome="failed",
                error=str(e),
            )
        await asyncio.sleep(poll_interval)


async def cleanup_phantom_topics(
    *,
    repo: Repository,
    tg: TelegramBridgePort,
) -> dict[str, int]:
    finder = getattr(repo, "find_phantom_topic_bindings", None)
    if not callable(finder):
        return {"found": 0, "deleted": 0, "closed": 0, "disabled": 0}
    bindings = await finder()
    stats = {"found": len(bindings), "deleted": 0, "closed": 0, "disabled": 0}
    for binding in bindings:
        flow_id = build_max_flow_id(binding.max_chat_id, "phantom-cleanup")
        deleted = False
        delete_topic = getattr(tg, "delete_topic", None)
        if callable(delete_topic):
            deleted = bool(await delete_topic(binding.tg_topic_id, flow_id=flow_id))
        if deleted:
            stats["deleted"] += 1
        else:
            close_topic = getattr(tg, "close_topic", None)
            if callable(close_topic) and await close_topic(binding.tg_topic_id, flow_id=flow_id):
                stats["closed"] += 1

        await repo.update_mode(binding.max_chat_id, "disabled")
        await repo.update_title(
            binding.max_chat_id,
            f"[deleted phantom] {binding.title}",
        )
        stats["disabled"] += 1
        log_event(
            logger,
            logging.INFO,
            "bridge.phantom_topic.cleaned",
            flow_id=flow_id,
            stage="maintenance",
            outcome="cleaned",
            max_chat_id=binding.max_chat_id,
            tg_topic_id=binding.tg_topic_id,
            deleted=deleted,
        )
    return stats


async def run_cleanup(
    *,
    cfg: AppConfig,
    repo: Repository,
    health: Optional[RuntimeHealthStore],
):
    while True:
        await asyncio.sleep(1800)
        try:
            await repo.cleanup_old_messages(cfg.bridge.message_retention_days)
            await repo.cleanup_old_logs(cfg.bridge.log_retention_days)
            if health is not None:
                await health.mark_healthy(
                    "storage",
                    summary="SQLite storage отвечает и cleanup проходит штатно",
                    notify=False,
                )
                await health.mark_healthy(
                    "scheduler",
                    summary="Cleanup scheduler работает штатно",
                    notify=False,
                )
            log_event(
                logger,
                logging.INFO,
                "bridge.cleanup.completed",
                stage="maintenance",
                outcome="completed",
                message_retention_days=cfg.bridge.message_retention_days,
                log_retention_days=cfg.bridge.log_retention_days,
            )
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "bridge.cleanup.failed",
                stage="maintenance",
                outcome="failed",
                error=str(e),
            )
            if health is not None:
                await health.report_issue(
                    "storage",
                    code="cleanup_failed",
                    summary="Cleanup старых записей в storage завершился ошибкой",
                    raw_cause=str(e),
                    severity=Severity.ERROR,
                    impact="Retention cleanup не выполнен; data/ может разрастаться и health-state устаревать.",
                    operator_hint="Проверь SQLite права/целостность и свободное место на диске.",
                    auto_recovery="Следующая попытка cleanup будет автоматически через 30 минут.",
                    notify=False,
                )


async def run_weekly_recovery_snapshot(
    *,
    safe_scan: Callable[..., Awaitable[dict[str, object]]],
    health: Optional[RuntimeHealthStore],
    log_scan_failure: Callable[..., None],
    interval_seconds: int = 7 * 24 * 3600,
):
    """Periodic recovery registry refresh. Default cadence: weekly."""
    await asyncio.sleep(max(1, int(interval_seconds)))
    while True:
        try:
            await safe_scan(reason="weekly", notify=True)
            if health is not None:
                await health.mark_healthy(
                    "scheduler",
                    summary="Weekly MAX recovery snapshot обновляется",
                    notify=False,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_scan_failure(reason="weekly", error=e)
            if health is not None:
                await health.report_issue(
                    "scheduler",
                    code="recovery_snapshot_failed",
                    summary="Weekly MAX recovery snapshot не обновился",
                    raw_cause=type(e).__name__,
                    severity=Severity.WARNING,
                    impact="Recovery registry может устареть до следующей успешной попытки.",
                    operator_hint="Проверь MAX-сессию и выполни /recovery scan вручную.",
                    auto_recovery="Scheduler повторит weekly snapshot на следующем цикле.",
                    notify=False,
                )
        await asyncio.sleep(max(1, int(interval_seconds)))
