"""
Bridge Core — runtime coordinator for MAX ↔ Telegram routing.

Transport-specific behavior stays in adapters. Business leaves live in bridge
modules; this class wires callbacks, shared dependencies and background tasks.
"""

import logging
import time
from pathlib import Path
from typing import Optional

from . import background as bridge_background
from . import delivery as bridge_delivery
from . import forwarding as bridge_forwarding
from . import inbound_retry as bridge_inbound_retry
from . import media_retry as bridge_media_retry
from . import outbound_retry as bridge_outbound_retry
from . import replies as bridge_replies
from . import status as bridge_status
from . import topics as bridge_topics
from .commands.dispatcher import BridgeCommandDispatcher
from .contracts import (
    MAX_DM_SWEEP_BACKFILL_SECONDS,
    MaxAttachment,
    MaxAttachmentFailure,
    MaxBridgePort,
    MaxMessage,
    MaxReactionUpdate,
    MaxTypingEvent,
    OpsNotifierPort,
    TelegramBridgePort,
)
from .recovery.scheduler import RecoveryScheduler
from ..config.loader import AppConfig
from ..db.repository import Repository
from ..runtime.health import RuntimeHealthStore, build_operator_alert
from ..runtime.health.metrics import run_runtime_metrics_textfile

logger = logging.getLogger(__name__)


class BridgeCore:
    def __init__(
        self,
        config: AppConfig,
        repo: Repository,
        max_adapter: MaxBridgePort,
        tg_adapter: TelegramBridgePort,
        ops_notifier: Optional[OpsNotifierPort] = None,
        health_store: Optional[RuntimeHealthStore] = None,
    ):
        self._cfg = config
        self._repo = repo
        self._max = max_adapter
        self._tg = tg_adapter
        self._ops = ops_notifier or tg_adapter
        self._health = health_store

        self._stats = {
            "start_time": time.time(),
            "inbound_text": 0,
            "inbound_media": 0,
            "outbound_text": 0,
            "outbound_media": 0,
            "failed_inbound": 0,
            "failed_outbound": 0,
        }

        self._recovery = RecoveryScheduler(
            cfg=self._cfg,
            max_adapter=self._max,
            tg=self._tg,
            repo=self._repo,
            health=self._health,
            send_ops_notification=self._send_ops_notification,
            format_duration_compact=bridge_forwarding.format_duration_compact,
        )
        self._status = bridge_status.BridgeStatusReporter(
            repo=self._repo,
            max_adapter=self._max,
            stats=self._stats,
            health=self._health,
            build_recovery_status_summary=self._recovery.build_status_summary,
        )
        self._commands = BridgeCommandDispatcher(
            tg=self._tg,
            repo=self._repo,
            max_adapter=self._max,
            status_reporter=self._status,
            recovery_scheduler=self._recovery,
        )

        self._max.on_message(self._on_max_message)
        self._max.on_typing(self._on_max_typing)
        self._max.on_reaction_update(self._on_max_reaction_update)
        self._tg.on_reply(self._on_tg_reply)
        self._commands.register()

        self._max.on_start(self._recovery.schedule_scan_after_connect)

    async def _send_ops_notification(self, text: str):
        sender = getattr(self._ops, "send_system_notification", None)
        if callable(sender):
            await sender(text)
            return
        await self._ops.send_notification(text)

    async def _emit_health_alert(self, change):
        if change is None or not getattr(change, "notify", False):
            return
        await self._send_ops_notification(build_operator_alert(change))

    async def _on_max_typing(self, event: MaxTypingEvent) -> None:
        """MAX пользователь начал печатать → отправляем typing в связанный топик."""
        binding = await self._repo.get_binding(event.chat_id)
        if binding is None:
            return
        await self._tg.send_typing_indicator(binding.tg_topic_id)

    async def _on_max_reaction_update(self, event: MaxReactionUpdate) -> None:
        """Обновление реакции в MAX → обновляем footer реакций в TG-сообщении."""
        tg_msg_id = await self._repo.get_tg_msg_by_max(event.chat_id, event.message_id)
        if tg_msg_id is None:
            return
        if not event.counters:
            return
        parts = [f"{c['emoji']} {c['count']}" for c in event.counters if c.get("emoji")]
        if not parts:
            return
        footer = "  ".join(parts)
        if event.actor_name:
            actor_footer = f"Последняя реакция: {event.actor_name}"
            if event.reaction:
                actor_footer = f"{actor_footer} — {event.reaction}"
            footer = f"{footer}\n{actor_footer}"
        await self._tg.edit_message_text(tg_msg_id, footer)

    async def _on_max_message(self, msg: MaxMessage):
        """Входящее сообщение из MAX → форвардим в Telegram."""
        await bridge_forwarding.handle_max_message(
            repo=self._repo,
            stats=self._stats,
            msg=msg,
            get_or_create_topic=self._get_or_create_topic,
            message_has_control_event=self._recovery.message_has_control_event,
            schedule_recovery_event_scan=self._recovery.schedule_event_scan,
            enqueue_retryable_media_failures=self._enqueue_media_retries,
            forward_to_telegram_fn=self._forward_to_telegram,
            get_last_tg_send_error=self._get_last_tg_send_error,
        )

    async def _enqueue_media_retries(
        self,
        msg: MaxMessage,
        topic_id: int,
        *,
        flow_id: str | None = None,
    ) -> tuple[int, list[MaxAttachmentFailure]]:
        return await bridge_media_retry.enqueue_retryable_media_failures(
            repo=self._repo,
            msg=msg,
            topic_id=topic_id,
            flow_id=flow_id,
        )

    async def _get_or_create_topic(
        self,
        msg: MaxMessage,
        *,
        flow_id: Optional[str] = None,
    ) -> Optional[int]:
        return await bridge_topics.get_or_create_topic(
            cfg=self._cfg,
            repo=self._repo,
            tg=self._tg,
            max_adapter=self._max,
            msg=msg,
            schedule_recovery_scan=self._recovery.schedule_event_scan,
            flow_id=flow_id,
        )

    async def _resolve_chat_title(self, msg: MaxMessage) -> str:
        return await bridge_topics.resolve_chat_title(
            cfg=self._cfg,
            max_adapter=self._max,
            msg=msg,
        )

    def _compose_message_text(self, primary: str, secondary: str = "") -> str:
        return bridge_forwarding.compose_message_text(primary, secondary)

    def _compose_attachment_failure_text(
        self,
        failures: list[MaxAttachmentFailure],
    ) -> str:
        return bridge_forwarding.compose_attachment_failure_text(failures)

    def _build_failed_outbound_id(self, topic_id: int, tg_msg_id: Optional[int]) -> str:
        return bridge_delivery.build_failed_outbound_id(topic_id, tg_msg_id)

    async def _log_outbound_failure(
        self,
        *,
        topic_id: int,
        tg_msg_id: Optional[int],
        max_chat_id: str,
        error: str,
        attempts: int = 1,
    ):
        await bridge_delivery.log_outbound_failure(
            self._repo,
            topic_id=topic_id,
            tg_msg_id=tg_msg_id,
            max_chat_id=max_chat_id,
            error=error,
            attempts=attempts,
        )

    def _is_file_too_large(self, path: str) -> bool:
        return bridge_forwarding.is_file_too_large(self._cfg, path)

    async def _send_attachment(
        self,
        topic_id: int,
        attachment: MaxAttachment,
        caption: str,
        *,
        flow_id: Optional[str] = None,
    ) -> Optional[int]:
        return await bridge_forwarding.send_attachment(
            cfg=self._cfg,
            tg=self._tg,
            topic_id=topic_id,
            attachment=attachment,
            caption=caption,
            flow_id=flow_id,
        )

    async def _forward_to_telegram(
        self,
        msg: MaxMessage,
        topic_id: int,
        *,
        flow_id: Optional[str] = None,
        attachment_failures: Optional[list[MaxAttachmentFailure]] = None,
    ) -> Optional[int]:
        return await bridge_forwarding.forward_to_telegram(
            cfg=self._cfg,
            tg=self._tg,
            msg=msg,
            topic_id=topic_id,
            flow_id=flow_id,
            attachment_failures=attachment_failures,
        )

    def _compose_tg_outbound_text(self, text: str, sender_name: Optional[str]) -> str:
        return bridge_replies.compose_tg_outbound_text(text, sender_name)

    async def _on_tg_reply(
        self,
        topic_id: int,
        tg_msg_id: Optional[int],
        text: str,
        reply_to_tg_msg_id: Optional[int],
        sender_name: Optional[str],
        media_path: Optional[str] = None,
        media_type: Optional[str] = None,
    ):
        await bridge_replies.handle_tg_reply(
            cfg=self._cfg,
            repo=self._repo,
            max_adapter=self._max,
            tg=self._tg,
            stats=self._stats,
            send_ops_notification=self._send_ops_notification,
            topic_id=topic_id,
            tg_msg_id=tg_msg_id,
            text=text,
            reply_to_tg_msg_id=reply_to_tg_msg_id,
            sender_name=sender_name,
            media_path=media_path,
            media_type=media_type,
        )

    async def run_pending_media_downloads(
        self,
        poll_interval: int = 60,
        lease_seconds: int = 600,
    ):
        await bridge_media_retry.run_pending_media_downloads(
            repo=self._repo,
            cfg=self._cfg,
            max_adapter=self._max,
            tg=self._tg,
            poll_interval=poll_interval,
            lease_seconds=lease_seconds,
        )

    def _get_last_tg_send_error(self) -> Optional[str]:
        getter = getattr(self._tg, "get_last_send_error", None)
        if callable(getter):
            return getter()
        return None

    async def run_pending_inbound_messages(
        self,
        poll_interval: int = bridge_inbound_retry.TEXT_RETRY_POLL_SECONDS,
        lease_seconds: int = bridge_inbound_retry.TEXT_RETRY_LEASE_SECONDS,
        ttl_seconds: int = bridge_inbound_retry.TEXT_RETRY_TTL_SECONDS,
    ):
        await bridge_inbound_retry.run_pending_inbound_messages(
            repo=self._repo,
            tg=self._tg,
            stats=self._stats,
            send_ops_notification=self._send_ops_notification,
            poll_interval=poll_interval,
            lease_seconds=lease_seconds,
            ttl_seconds=ttl_seconds,
        )

    async def run_pending_outbound_messages(
        self,
        poll_interval: int = bridge_outbound_retry.PENDING_OUTBOUND_POLL_SECONDS,
        lease_seconds: int = bridge_outbound_retry.PENDING_OUTBOUND_LEASE_SECONDS,
        ttl_seconds: int = bridge_outbound_retry.PENDING_OUTBOUND_TTL_SECONDS,
    ):
        await bridge_outbound_retry.run_pending_outbound_messages(
            repo=self._repo,
            max_adapter=self._max,
            tg=self._tg,
            stats=self._stats,
            send_ops_notification=self._send_ops_notification,
            poll_interval=poll_interval,
            lease_seconds=lease_seconds,
            ttl_seconds=ttl_seconds,
        )

    async def run_dm_history_sweep(
        self,
        poll_interval: int | None = None,
        limit: int | None = None,
        backfill_seconds: int | None = None,
    ):
        sweep_cfg = getattr(getattr(self._cfg, "health", None), "dm_history_sweep", None)
        await bridge_background.run_dm_history_sweep(
            repo=self._repo,
            max_adapter=self._max,
            poll_interval=poll_interval,
            enabled=getattr(sweep_cfg, "enabled", True),
            warmup_seconds=getattr(sweep_cfg, "warmup_seconds", 10 * 60),
            warmup_interval_seconds=getattr(sweep_cfg, "warmup_interval_seconds", 120),
            steady_interval_seconds=getattr(sweep_cfg, "steady_interval_seconds", 15 * 60),
            limit=limit if limit is not None else getattr(sweep_cfg, "limit", 30),
            backfill_seconds=(
                backfill_seconds
                if backfill_seconds is not None
                else getattr(sweep_cfg, "backfill_seconds", MAX_DM_SWEEP_BACKFILL_SECONDS)
            ),
            cycle_jitter_seconds=getattr(sweep_cfg, "cycle_jitter_seconds", 30),
            per_chat_delay_seconds=getattr(sweep_cfg, "per_chat_delay_seconds", 0.5),
        )

    async def cleanup_phantom_topics(self) -> dict[str, int]:
        return await bridge_background.cleanup_phantom_topics(
            repo=self._repo,
            tg=self._tg,
        )

    async def run_weekly_recovery_snapshot(self, interval_seconds: int = 7 * 24 * 3600):
        await self._recovery.run_weekly_snapshot(interval_seconds=interval_seconds)

    async def run_periodic_status(self, interval_hours: int = 4):
        """Автоматически отправлять статусный отчёт каждые interval_hours часов."""
        await bridge_background.run_periodic_status(
            health=self._health,
            build_status_message=self._status.build_status_message,
            send_ops_notification=self._send_ops_notification,
            interval_hours=interval_hours,
        )

    async def run_metrics_textfile(self):
        health_cfg = getattr(self._cfg, "health", None)
        await run_runtime_metrics_textfile(
            path=getattr(health_cfg, "metrics_textfile_path", None),
            health=self._health,
            repo=self._repo,
            interval_seconds=getattr(health_cfg, "metrics_interval_seconds", 30),
        )

    async def fix_fallback_titles(self):
        await bridge_topics.fix_fallback_titles(
            repo=self._repo,
            tg=self._tg,
            max_adapter=self._max,
            schedule_recovery_scan=self._recovery.schedule_event_scan,
        )

    async def run_max_watchdog(
        self,
        alert_after_seconds: int = 60,
        check_interval: int = 10,
    ):
        """Фоновая задача: следит за доступностью MAX."""
        health_cfg = getattr(self._cfg, "health", None)
        storage_cfg = getattr(self._cfg, "storage", None)
        data_dir = getattr(storage_cfg, "data_dir", None)
        self_heal_state_path = (
            Path(data_dir) / "max_egress_self_heal.json" if data_dir is not None else None
        )
        await bridge_background.run_max_watchdog(
            max_adapter=self._max,
            health=self._health,
            send_ops_notification=self._send_ops_notification,
            emit_health_alert=self._emit_health_alert,
            alert_after_seconds=alert_after_seconds,
            check_interval=check_interval,
            egress_probe_interval=getattr(health_cfg, "max_egress_probe_interval_seconds", 30),
            egress_startup_grace_seconds=getattr(
                health_cfg,
                "max_egress_startup_grace_seconds",
                15 * 60,
            ),
            self_heal_grace_seconds=getattr(health_cfg, "max_self_heal_grace_seconds", 180),
            self_heal_restart_cooldown_seconds=getattr(
                health_cfg,
                "max_self_heal_restart_cooldown_seconds",
                1800,
            ),
            self_heal_state_path=self_heal_state_path,
        )

    async def run_cleanup(self):
        """Периодическая очистка старых записей. Запускать в фоне."""
        await bridge_background.run_cleanup(
            cfg=self._cfg,
            repo=self._repo,
            health=self._health,
        )
