"""Supervisor loop that keeps the container alive and restarts the bridge worker."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .health import RuntimeHealthStore, Severity, build_operator_alert, humanize_duration

logger = logging.getLogger(__name__)

WorkerFactory = Callable[[], Awaitable[None]]
Notifier = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class SupervisorConfig:
    heartbeat_interval_seconds: int = 30
    worker_restart_backoff_seconds: int = 5


class BridgeSupervisor:
    def __init__(self,
                 *,
                 health_store: RuntimeHealthStore,
                 worker_factory: WorkerFactory,
                 notify: Optional[Notifier] = None,
                 config: SupervisorConfig = SupervisorConfig()):
        self._health = health_store
        self._worker_factory = worker_factory
        self._notify = notify
        self._config = config

    async def run(self):
        await self._health.set_supervisor_started()
        heartbeat_task = asyncio.create_task(self._run_heartbeat(), name="supervisor_heartbeat")

        try:
            while True:
                await self._health.mark_recovering(
                    "runtime",
                    summary="Supervisor запускает bridge worker",
                    notify=False,
                )

                try:
                    await self._worker_factory()
                    error: BaseException = RuntimeError("bridge worker exited without exception")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = exc

                await self._handle_worker_failure(error)

                backoff = max(1, int(self._config.worker_restart_backoff_seconds))
                jitter = random.uniform(0.0, min(1.0, backoff / 2))
                await asyncio.sleep(backoff + jitter)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _run_heartbeat(self):
        interval = max(1, int(self._config.heartbeat_interval_seconds))
        while True:
            await self._health.write_heartbeat()
            await asyncio.sleep(interval)

    async def _handle_worker_failure(self, error: BaseException):
        raw_cause = str(error).strip() or error.__class__.__name__
        backoff = max(1, int(self._config.worker_restart_backoff_seconds))
        await self._health.increment_worker_restarts()

        change = await self._health.report_issue(
            "runtime",
            code="worker_crashed",
            summary="Bridge worker аварийно завершился и будет перезапущен",
            raw_cause=raw_cause,
            severity=Severity.ERROR,
            impact="Контейнер остаётся Up, supervisor сам поднимет worker заново.",
            operator_hint="Проверь /status и свежие логи. Если проблема связана с MAX session, сделай reauth по SMS.",
            auto_recovery=f"Новый запуск через ~{humanize_duration(backoff)}.",
            notify=True,
        )

        subsystem, code, summary, impact, operator_hint = _classify_worker_error(error)
        if subsystem != "runtime":
            await self._health.report_issue(
                subsystem,
                code=code,
                summary=summary,
                raw_cause=raw_cause,
                severity=Severity.ERROR,
                impact=impact,
                operator_hint=operator_hint,
                auto_recovery=f"Supervisor перезапустит worker через ~{humanize_duration(backoff)}.",
                notify=False,
            )

        logger.error("Bridge worker crashed and will be restarted: %s", raw_cause)

        if self._notify is not None and change.notify:
            try:
                await self._notify(build_operator_alert(change))
            except Exception:
                logger.exception("Supervisor notification failed after worker crash")


def _classify_worker_error(error: BaseException) -> tuple[str, str, str, str, str]:
    type_name = error.__class__.__name__.lower()
    raw = str(error).lower()

    if "telegram" in type_name or "telegram" in raw or "aiogram" in raw:
        return (
            "tg_link",
            "telegram_worker_failed",
            "Telegram polling или Telegram transport остановился",
            "Команды бота и доставка bridge-уведомлений через Telegram могут временно не работать.",
            "Проверь bot token, доступность Telegram API и нет ли конфликтующего polling/webhook.",
        )

    if "sqlite" in type_name or "sqlite" in raw or "database" in raw:
        return (
            "storage",
            "storage_unavailable",
            "SQLite storage недоступен или повреждён",
            "Bridge не может читать/писать mapping, delivery log и runtime health state.",
            "Проверь файловую систему, права доступа и целостность SQLite файлов в data/.",
        )

    return (
        "runtime",
        "worker_crashed",
        "Bridge worker аварийно завершился",
        "Bridge временно недоступен до автоперезапуска worker.",
        "Проверь /status и логи после рестарта worker.",
    )
