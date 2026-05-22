"""Runtime composition root for bridge adapters and background tasks."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.adapters.max_adapter import MaxAdapter
from src.adapters.tg_adapter import TelegramAdapter
from src.bridge.contracts import MaxIssue
from src.bridge.core import BridgeCore
from src.config.loader import AppConfig
from src.db.repository import Repository
from src.logging_utils import log_event
from src.runtime.health import RuntimeHealthStore, Severity, build_operator_alert


@dataclass
class OpsNotifierRuntime:
    notifier: TelegramAdapter | None
    outbox_task: asyncio.Task | None = None


async def _emit_health_change(notifier: TelegramAdapter | None, change):
    if notifier is None or change is None or not getattr(change, "notify", False):
        return
    await notifier.send_system_notification(build_operator_alert(change), category="health")


def _build_max_health_payload(issue: MaxIssue) -> dict:
    operator_hint = (
        "Сделай reauth по SMS: перезапусти bridge и введи новый код."
        if issue.requires_reauth
        else "Проверь /status и логи MAX. Если reconnect не проходит, попробуй перезапуск bridge."
    )
    auto_recovery = (
        "Bridge оставляет контейнер Up и продолжит reconnect, но без reauth восстановление маловероятно."
        if issue.requires_reauth
        else "MAX reconnect loop уже активен и будет продолжать попытки автоматически."
    )
    return {
        "code": issue.kind,
        "summary": issue.summary,
        "raw_cause": issue.raw_error,
        "severity": Severity.CRITICAL if issue.requires_reauth else Severity.ERROR,
        "impact": "Связка MAX ↔ Telegram деградировала: входящие и/или исходящие сообщения могут не проходить.",
        "operator_hint": operator_hint,
        "auto_recovery": auto_recovery,
        "requires_reauth": issue.requires_reauth,
    }


async def setup_ops_notifier(
    cfg: AppConfig,
    health_store: RuntimeHealthStore,
    logger: logging.Logger,
) -> OpsNotifierRuntime:
    ops_notifier: TelegramAdapter | None = None
    outbox_task: asyncio.Task | None = None
    try:
        ops_notifier = TelegramAdapter(
            bot_token=cfg.telegram.bot_token,
            owner_id=cfg.telegram.owner_id,
            forum_group_id=cfg.telegram.forum_group_id,
            ops_topic_id=cfg.telegram.ops_topic_id,
            tmp_dir=str(cfg.storage.tmp_dir),
            outbox_store=health_store.outbox,
            health_store=health_store,
        )
        await ops_notifier.setup()
        await health_store.mark_healthy(
            "alerting",
            summary="Telegram ops notifier инициализирован",
            notify=False,
        )
        outbox_task = asyncio.create_task(
            ops_notifier.run_notification_outbox(
                poll_interval_seconds=max(5, cfg.health.heartbeat_interval_seconds)
            ),
            name="ops_notification_outbox",
        )
    except Exception as e:
        logger.error("Ops notifier setup failed: %s", e, exc_info=True)
        await health_store.report_issue(
            "alerting",
            code="notifier_setup_failed",
            summary="Telegram ops notifier не инициализировался",
            raw_cause=str(e),
            severity=Severity.ERROR,
            impact="Живые ops-алерты могут не отправляться, но будут копиться в outbox при следующих попытках.",
            operator_hint="Проверь bot token и доступность Telegram API.",
            auto_recovery="После починки notifier начнёт автоматически досылать накопленный outbox.",
            notify=False,
        )
        ops_notifier = None
    return OpsNotifierRuntime(notifier=ops_notifier, outbox_task=outbox_task)


async def close_ops_notifier(runtime: OpsNotifierRuntime):
    if runtime.outbox_task is not None:
        runtime.outbox_task.cancel()
        try:
            await runtime.outbox_task
        except asyncio.CancelledError:
            pass
    if runtime.notifier is not None:
        await runtime.notifier.close()


async def run_bridge_worker(
    cfg: AppConfig,
    health_store: RuntimeHealthStore,
    notifier: TelegramAdapter | None,
    logger: logging.Logger,
    *,
    startup_tests_runner: Callable[[logging.Logger], Awaitable[Any]],
    startup_notification_builder: Callable[..., Awaitable[str]],
):
    repo: Repository | None = None
    tg_adapter: TelegramAdapter | None = None
    stage = "storage_connect"

    try:
        repo = Repository(cfg.storage.db_path)
        await repo.connect()
        await health_store.mark_healthy(
            "storage",
            summary="SQLite storage подключён и доступен",
            notify=False,
        )
        log_event(
            logger,
            logging.INFO,
            "app.startup.db_connected",
            stage="startup",
            outcome="ok",
            db_path=Path(cfg.storage.db_path).name,
        )

        max_adapter = MaxAdapter(
            phone=cfg.max.phone,
            data_dir=cfg.storage.session_path,
            session_name=cfg.max.session_filename,
            tmp_dir=str(cfg.storage.tmp_dir),
        )

        tg_adapter = TelegramAdapter(
            bot_token=cfg.telegram.bot_token,
            owner_id=cfg.telegram.owner_id,
            forum_group_id=cfg.telegram.forum_group_id,
            tmp_dir=str(cfg.storage.tmp_dir),
        )

        system_notifier = notifier or tg_adapter
        bridge = BridgeCore(
            cfg,
            repo,
            max_adapter,
            tg_adapter,
            ops_notifier=system_notifier,
            health_store=health_store,
        )

        stage = "max_callbacks"
        started_once = False

        async def on_max_ready():
            nonlocal started_once
            change = await health_store.mark_healthy(
                "max_link",
                summary="MAX connected and synchronized",
                notify=True,
            )
            await _emit_health_change(system_notifier, change)

            if started_once:
                return
            started_once = True
            startup_tests = await startup_tests_runner(logger)
            await system_notifier.send_system_notification(
                await startup_notification_builder(repo, startup_tests=startup_tests),
                category="startup",
            )

        async def on_max_issue(issue: MaxIssue):
            payload = _build_max_health_payload(issue)
            change = await health_store.report_issue(
                "max_link",
                code=payload["code"],
                summary=payload["summary"],
                raw_cause=payload["raw_cause"],
                severity=payload["severity"],
                impact=payload["impact"],
                operator_hint=payload["operator_hint"],
                auto_recovery=payload["auto_recovery"],
                requires_reauth=payload["requires_reauth"],
                notify=True,
            )
            await _emit_health_change(system_notifier, change)

        max_adapter.on_start(on_max_ready)
        max_adapter.on_issue(on_max_issue)

        stage = "tg_setup"
        await tg_adapter.setup()
        await health_store.mark_healthy(
            "tg_link",
            summary="Telegram polling adapter инициализирован",
            notify=False,
        )
        await health_store.mark_healthy(
            "scheduler",
            summary="Background scheduler initialized",
            notify=False,
        )

        runtime_change = await health_store.mark_healthy(
            "runtime",
            summary="Bridge worker запущен и держит task group",
            notify=True,
        )
        await _emit_health_change(system_notifier, runtime_change)

        bot = tg_adapter.get_bot()
        dp = tg_adapter.get_dispatcher()

        log_event(
            logger,
            logging.INFO,
            "app.startup.bridge_starting",
            stage="startup",
            outcome="started",
        )

        stage = "task_group"
        async with asyncio.TaskGroup() as tg:
            tg.create_task(max_adapter.start(), name="max_adapter")
            tg.create_task(
                dp.start_polling(bot, allowed_updates=["message"]),
                name="tg_polling",
            )
            tg.create_task(bridge.run_cleanup(), name="cleanup")
            tg.create_task(
                bridge.run_pending_media_downloads(),
                name="pending_media_downloads",
            )
            tg.create_task(
                bridge.run_dm_history_sweep(),
                name="dm_history_sweep",
            )
            tg.create_task(bridge.run_max_watchdog(), name="max_watchdog")
            tg.create_task(
                bridge.run_periodic_status(cfg.health.reminder_interval_hours),
                name="periodic_status",
            )
            tg.create_task(
                bridge.run_weekly_recovery_snapshot(),
                name="weekly_recovery_snapshot",
            )
    except Exception as e:
        if stage == "storage_connect":
            await health_store.report_issue(
                "storage",
                code="storage_connect_failed",
                summary="SQLite storage не удалось подключить",
                raw_cause=str(e),
                severity=Severity.ERROR,
                impact="Bridge не может стартовать worker без storage.",
                operator_hint="Проверь путь DATA_DIR, права доступа и целостность SQLite файлов.",
                auto_recovery="Supervisor попытается перезапустить worker автоматически.",
                notify=False,
            )
        elif stage == "tg_setup":
            await health_store.report_issue(
                "tg_link",
                code="telegram_setup_failed",
                summary="Telegram polling adapter не инициализировался",
                raw_cause=str(e),
                severity=Severity.ERROR,
                impact="Команды бота и bridge-алерты через Telegram временно недоступны.",
                operator_hint="Проверь bot token, доступность Telegram API и конфиг forum_group_id.",
                auto_recovery="Supervisor попытается перезапустить worker автоматически.",
                notify=False,
            )
        raise
    finally:
        if tg_adapter is not None:
            await tg_adapter.close()
        if repo is not None:
            await repo.close()
