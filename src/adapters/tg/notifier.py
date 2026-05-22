"""Telegram system notification fanout and alert outbox handling."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Optional

from ...runtime.health import (
    AlertOutboxStore,
    OutboxMessage,
    RuntimeHealthStore,
    Severity,
)

logger = logging.getLogger("src.adapters.tg_adapter")

SystemMessageSender = Callable[[str, int, Optional[int], str], Awaitable[tuple[bool, str]]]


class TelegramNotifier:
    def __init__(
        self,
        *,
        owner_id: int,
        forum_group_id: int,
        ops_topic_id: Optional[int] = None,
        outbox_store: Optional[AlertOutboxStore] = None,
        health_store: Optional[RuntimeHealthStore] = None,
        send_system_message: SystemMessageSender,
    ):
        self._owner_id = owner_id
        self._group_id = forum_group_id
        self._ops_topic_id = ops_topic_id
        self._outbox = outbox_store
        self._health = health_store
        self._send_system_message = send_system_message

    async def send_system_notification(self, text: str, *, category: str = "system") -> bool:
        """Send a system notification to all ops targets and queue failures."""
        await self.flush_notification_outbox()

        failures: list[tuple[str, int, Optional[int], str]] = []
        for label, chat_id, message_thread_id in self._iter_notification_targets():
            ok, error_text = await self._send_system_message(
                text,
                chat_id,
                message_thread_id,
                label,
            )
            if not ok:
                failures.append((label, chat_id, message_thread_id, error_text))

        if failures:
            if self._outbox is not None:
                for label, chat_id, message_thread_id, error_text in failures:
                    await self._outbox.queue(
                        OutboxMessage(
                            id=f"{int(time.time())}-{chat_id}-{message_thread_id or 0}-{label}",
                            text=text,
                            chat_id=chat_id,
                            message_thread_id=message_thread_id,
                            label=label,
                            category=category,
                            created_at=int(time.time()),
                            attempts=1,
                            last_error=error_text,
                        )
                    )
            await self._report_alerting_issue(failures)
            return False

        await self._mark_alerting_healthy()
        return True

    async def send_notification(self, text: str) -> bool:
        """Backwards-compatible alias for ops/system notifications."""
        return await self.send_system_notification(text)

    async def flush_notification_outbox(self, *, limit: int = 100) -> int:
        if self._outbox is None:
            return 0

        items = await self._outbox.load()
        if not items:
            return 0

        delivered = 0
        remaining: list[OutboxMessage] = []
        for index, item in enumerate(items):
            if index >= limit:
                remaining.extend(items[index:])
                break

            ok, error_text = await self._send_system_message(
                item.text,
                item.chat_id,
                item.message_thread_id,
                item.label,
            )
            if ok:
                delivered += 1
                continue

            item.attempts += 1
            item.last_error = error_text
            remaining.append(item)

        await self._outbox.rewrite(remaining)
        if remaining:
            await self._report_alerting_issue(
                [(item.label, item.chat_id, item.message_thread_id, item.last_error) for item in remaining]
            )
        elif delivered:
            await self._mark_alerting_healthy()
        return delivered

    async def run_notification_outbox(self, *, poll_interval_seconds: int = 30):
        while True:
            await asyncio.sleep(max(1, int(poll_interval_seconds)))
            try:
                await self.flush_notification_outbox()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("notification outbox flush failed: %s", e, exc_info=True)

    def _iter_notification_targets(self):
        yield ("owner_dm", self._owner_id, None)
        if self._ops_topic_id is not None:
            yield ("ops_topic", self._group_id, self._ops_topic_id)

    async def _report_alerting_issue(
        self,
        failures: list[tuple[str, int, Optional[int], str]],
    ):
        if self._health is None:
            return
        labels = ", ".join(sorted({label for label, *_ in failures}))
        errors = "; ".join(error for *_, error in failures if error)
        await self._health.report_issue(
            "alerting",
            code="system_notification_failed",
            summary=f"Часть системных алертов не доставлена ({labels})",
            raw_cause=errors or "Telegram send failed",
            severity=Severity.ERROR,
            impact="Операторские уведомления частично ушли в alert_outbox и будут досылаться автоматически.",
            operator_hint="Проверь Telegram API, bot token и состояние data/alert_outbox.jsonl.",
            auto_recovery="Notifier продолжит автоматический retry outbox без перезапуска контейнера.",
            notify=False,
        )

    async def _mark_alerting_healthy(self):
        if self._health is None:
            return
        await self._health.mark_healthy(
            "alerting",
            summary="Системные алерты доставляются штатно",
            notify=False,
        )
