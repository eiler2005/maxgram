"""
Bridge Core — центральная логика роутинга.

MAX message → Telegram topic
Telegram reply → MAX message

Принципы:
  - Все решения здесь, адаптеры только транспорт
  - Deduplication по max_msg_id
  - Auto-create топик при первом сообщении из нового чата
  - DM чаты: резолвим имя через MAX API
  - Не хранить содержимое сообщений в логах
"""

import asyncio
import logging
import time
from typing import Optional

from . import background as bridge_background
from . import delivery as bridge_delivery
from . import forwarding as bridge_forwarding
from . import mapping as bridge_mapping
from . import media_retry as bridge_media_retry
from . import replies as bridge_replies
from . import topics as bridge_topics
from .commands import dm as bridge_dm_command
from .commands import recovery as bridge_recovery_command
from .contracts import (
    MAX_DM_SWEEP_BACKFILL_SECONDS,
    MaxAttachment,
    MaxAttachmentFailure,
    MaxBridgePort,
    MaxMessage,
    OpsNotifierPort,
    TelegramBridgePort,
)
from .recovery import orchestrator as recovery_orchestrator
from .recovery import reporter as recovery_reporter
from ..config.loader import AppConfig
from ..db.repository import Repository, ChatBinding, PendingMediaDownload
from ..logging_utils import build_tg_flow_id, log_event, sanitize_path
from ..runtime.health import (
    RuntimeHealthStore,
    build_operator_alert,
    format_timestamp,
    render_health_summary,
)

logger = logging.getLogger(__name__)

RECOVERY_EVENT_SCAN_DELAYS = {
    "new_binding": 60,
    "title_changed": 30,
    "control_event": 120,
}
RECOVERY_EVENT_SCAN_COOLDOWNS = {
    "new_binding": 0,
    "title_changed": 5 * 60,
    "control_event": 15 * 60,
}


class BridgeCore:
    def __init__(self, config: AppConfig, repo: Repository,
                 max_adapter: MaxBridgePort, tg_adapter: TelegramBridgePort,
                 ops_notifier: Optional[OpsNotifierPort] = None,
                 health_store: Optional[RuntimeHealthStore] = None):
        self._cfg = config
        self._repo = repo
        self._max = max_adapter
        self._tg = tg_adapter
        self._ops = ops_notifier or tg_adapter
        self._health = health_store

        # Счётчики в памяти (накопительные с запуска)
        self._stats = {
            "start_time": time.time(),
            "inbound_text": 0,
            "inbound_media": 0,
            "outbound_text": 0,
            "outbound_media": 0,
            "failed_inbound": 0,
            "failed_outbound": 0,
        }

        # Регистрируем обработчики
        self._max.on_message(self._on_max_message)
        self._tg.on_reply(self._on_tg_reply)
        self._tg.on_command("status", self._build_status_message)
        self._tg.on_command("chats", self._build_chats_message)
        self._tg.on_command("help", self._build_help_message)
        self._tg.on_arg_command("dm", self._cmd_dm, allow_group_general=True)
        self._tg.on_arg_command("recovery", self._cmd_recovery)

        self._recovery_scan_task: Optional[asyncio.Task] = None
        self._recovery_event_scan_task: Optional[asyncio.Task] = None
        self._recovery_event_scan_at: Optional[float] = None
        self._recovery_event_scan_reasons: set[str] = set()
        self._recovery_event_last_scan_at: dict[str, float] = {}
        self._recovery_event_scan_delays = dict(RECOVERY_EVENT_SCAN_DELAYS)
        self._recovery_event_scan_cooldowns = dict(RECOVERY_EVENT_SCAN_COOLDOWNS)
        self._recovery_scan_lock = asyncio.Lock()
        self._last_recovery_notification_digest: Optional[str] = None
        self._last_recovery_notification_at = 0.0
        on_max_start = getattr(self._max, "on_start", None)
        if callable(on_max_start):
            on_max_start(self._schedule_recovery_scan_after_connect)

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

    # ── MAX → Telegram ────────────────────────────────────────────────────

    async def _on_max_message(self, msg: MaxMessage):
        """Входящее сообщение из MAX → форвардим в Telegram."""
        await bridge_forwarding.handle_max_message(
            repo=self._repo,
            stats=self._stats,
            msg=msg,
            get_or_create_topic=self._get_or_create_topic,
            message_has_control_event=self._message_has_recovery_control_event,
            schedule_recovery_event_scan=self._schedule_recovery_event_scan,
            enqueue_retryable_media_failures=self._enqueue_retryable_media_failures,
            forward_to_telegram_fn=self._forward_to_telegram,
        )

    async def _enqueue_retryable_media_failures(
        self,
        msg: MaxMessage,
        topic_id: int,
        *,
        flow_id: Optional[str] = None,
    ) -> tuple[int, list[MaxAttachmentFailure]]:
        enqueued = 0
        display_failures: list[MaxAttachmentFailure] = []
        now = int(time.time())
        first_retry_at = now + 60
        for failure in msg.attachment_failures:
            if not self._is_retryable_media_failure(failure):
                display_failures.append(failure)
                continue

            existing = await self._find_existing_pending_media_for_failure(msg, failure)
            if existing is not None:
                log_event(
                    logger,
                    logging.INFO,
                    "bridge.media_retry.enqueued",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="media_retry",
                    outcome="existing",
                    reason="pending_media_already_exists",
                    max_chat_id=msg.chat_id,
                    max_msg_id=msg.msg_id,
                    tg_topic_id=topic_id,
                    pending_media_id=existing.id,
                    attachment_index=failure.index,
                    kind=failure.kind,
                    reference_kind=failure.reference_kind,
                )
                continue

            job_id = await self._repo.enqueue_pending_media(
                PendingMediaDownload(
                    max_chat_id=msg.chat_id,
                    max_msg_id=msg.msg_id,
                    tg_topic_id=topic_id,
                    attachment_index=failure.index,
                    kind=failure.kind,
                    source_type=failure.source_type,
                    media_chat_id=failure.media_chat_id or msg.chat_id,
                    media_msg_id=failure.media_msg_id or msg.msg_id,
                    reference_kind=failure.reference_kind or "video_id",
                    reference_id=failure.reference_id or "",
                    filename=failure.filename,
                    duration=failure.duration,
                    width=failure.width,
                    height=failure.height,
                    next_attempt_at=first_retry_at,
                    last_error=failure.reason,
                )
            )
            enqueued += 1
            display_failures.append(failure)
            log_event(
                logger,
                logging.INFO,
                "bridge.media_retry.enqueued",
                flow_id=flow_id,
                direction="inbound",
                stage="media_retry",
                outcome="enqueued",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=topic_id,
                pending_media_id=job_id,
                attachment_index=failure.index,
                kind=failure.kind,
                reference_kind=failure.reference_kind,
            )
        return enqueued, display_failures

    async def _find_existing_pending_media_for_failure(
        self,
        msg: MaxMessage,
        failure: MaxAttachmentFailure,
    ) -> Optional[PendingMediaDownload]:
        finder = getattr(self._repo, "find_active_pending_media", None)
        if callable(finder):
            existing = await finder(
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                attachment_index=failure.index,
                kind=failure.kind,
            )
            if existing is not None:
                return existing

        ref_finder = getattr(self._repo, "find_active_pending_media_by_reference", None)
        if (
            callable(ref_finder)
            and failure.reference_kind
            and failure.reference_id
            and (failure.media_chat_id or msg.chat_id)
            and (failure.media_msg_id or msg.msg_id)
        ):
            return await ref_finder(
                media_chat_id=failure.media_chat_id or msg.chat_id,
                media_msg_id=failure.media_msg_id or msg.msg_id,
                attachment_index=failure.index,
                kind=failure.kind,
                reference_kind=failure.reference_kind,
                reference_id=failure.reference_id,
            )
        return None

    async def _get_or_create_topic(self, msg: MaxMessage, *,
                                   flow_id: Optional[str] = None) -> Optional[int]:
        return await bridge_topics.get_or_create_topic(
            cfg=self._cfg,
            repo=self._repo,
            tg=self._tg,
            max_adapter=self._max,
            msg=msg,
            schedule_recovery_scan=self._schedule_recovery_event_scan,
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

    def _is_retryable_media_failure(self, failure: MaxAttachmentFailure) -> bool:
        return bridge_media_retry.is_retryable_media_failure(failure)

    def _pending_media_retry_delay(self, attempts_after_failure: int) -> int:
        return bridge_media_retry.pending_media_retry_delay(attempts_after_failure)

    def _format_duration_compact(self, seconds: int) -> str:
        return bridge_forwarding.format_duration_compact(seconds)

    def _build_failed_outbound_id(self, topic_id: int, tg_msg_id: Optional[int]) -> str:
        return bridge_delivery.build_failed_outbound_id(topic_id, tg_msg_id)

    async def _log_outbound_failure(self, *, topic_id: int, tg_msg_id: Optional[int],
                                    max_chat_id: str, error: str, attempts: int = 1):
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

    async def _send_attachment(self, topic_id: int, attachment: MaxAttachment,
                               caption: str, *, flow_id: Optional[str] = None) -> Optional[int]:
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

    # ── Telegram → MAX ────────────────────────────────────────────────────

    def _compose_tg_outbound_text(self, text: str, sender_name: Optional[str]) -> str:
        return bridge_replies.compose_tg_outbound_text(text, sender_name)

    async def _on_tg_reply(self, topic_id: int, tg_msg_id: Optional[int], text: str,
                           reply_to_tg_msg_id: Optional[int],
                           sender_name: Optional[str],
                           media_path: Optional[str] = None,
                           media_type: Optional[str] = None):
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

    # ── Pending media retry ────────────────────────────────────────────────

    async def _mark_pending_media_retry(
        self,
        job: PendingMediaDownload,
        *,
        error: str,
        flow_id: str,
    ):
        await bridge_media_retry.mark_pending_media_retry(
            repo=self._repo,
            job=job,
            error=error,
            flow_id=flow_id,
        )

    async def _process_pending_media_download(self, job: PendingMediaDownload):
        await bridge_media_retry.process_pending_media_download(
            cfg=self._cfg,
            repo=self._repo,
            max_adapter=self._max,
            tg=self._tg,
            job=job,
        )

    async def run_pending_media_downloads(
        self,
        poll_interval: int = 60,
        lease_seconds: int = 600,
    ):
        log_event(
            logger,
            logging.INFO,
            "bridge.media_retry.worker_started",
            stage="media_retry",
            outcome="started",
            poll_interval_seconds=poll_interval,
            lease_seconds=lease_seconds,
        )
        while True:
            try:
                now = int(time.time())
                jobs = await self._repo.get_due_pending_media(now=now, limit=5)
                for job in jobs:
                    if not job.id:
                        continue
                    leased = await self._repo.lease_pending_media(
                        job.id,
                        lease_until=now + lease_seconds,
                        now=now,
                    )
                    if not leased:
                        continue
                    await self._process_pending_media_download(job)
            except Exception as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "bridge.media_retry.worker_failed",
                    stage="media_retry",
                    outcome="failed",
                    error=str(e),
                )
            await asyncio.sleep(poll_interval)

    async def run_dm_history_sweep(
        self,
        poll_interval: int = 120,
        limit: int = 30,
        backfill_seconds: int = MAX_DM_SWEEP_BACKFILL_SECONDS,
    ):
        await bridge_background.run_dm_history_sweep(
            repo=self._repo,
            max_adapter=self._max,
            poll_interval=poll_interval,
            limit=limit,
            backfill_seconds=backfill_seconds,
        )

    async def cleanup_phantom_topics(self) -> dict[str, int]:
        return await bridge_background.cleanup_phantom_topics(
            repo=self._repo,
            tg=self._tg,
        )

    # ── Status report ─────────────────────────────────────────────────────

    async def _build_status_message(self, period_hours: int = 4) -> str:
        """Сформировать текстовый статусный отчёт за period_hours часов."""
        since = int(time.time()) - period_hours * 3600

        msgs = await self._repo.count_messages_since(since)
        deliveries = await self._repo.count_deliveries_since(since)
        chat_activity = await self._repo.get_chat_activity_since(since, limit=10)
        all_bindings = await self._repo.list_bindings()

        # Uptime
        uptime_sec = int(time.time() - self._stats["start_time"])
        h, m = divmod(uptime_sec // 60, 60)
        uptime_str = f"{h}ч {m}м" if h else f"{m}м"

        # Соединения
        max_ok = "✅" if self._max.is_ready() else "❌"
        tg_ok = "✅"  # если мы дошли до /status — TG работает
        get_last_issue = getattr(self._max, "get_last_issue", None)
        max_issue = get_last_issue() if callable(get_last_issue) else None
        get_last_connected_at = getattr(self._max, "get_last_connected_at", None)
        last_connected_at = get_last_connected_at() if callable(get_last_connected_at) else None

        # Сообщения за период
        inbound_total = msgs.get("inbound", 0)
        outbound_total = msgs.get("outbound", 0)
        inbound_media = self._stats["inbound_media"]
        outbound_media = self._stats["outbound_media"]
        failed_in = self._stats["failed_inbound"]
        failed_out = self._stats["failed_outbound"]
        errors_total = failed_in + failed_out

        # Чаты
        total_chats = len(all_bindings)
        active_chats = sum(1 for b in all_bindings if b.mode == "active")

        lines = [
            f"📊 Bridge Status  ·  uptime: {uptime_str}",
            f"Период: последние {period_hours}ч",
            "",
            "🔗 Соединение",
            f"  MAX → Telegram  {max_ok}",
            f"  Telegram → MAX  {tg_ok}",
            "",
            f"📨 Сообщения (за {period_hours}ч)",
            f"  Входящих  (MAX→TG): {inbound_total}"
            + (f"  (медиа: {inbound_media})" if inbound_media else ""),
            f"  Исходящих (TG→MAX): {outbound_total}"
            + (f"  (медиа: {outbound_media})" if outbound_media else ""),
        ]
        if errors_total:
            lines.append(f"  ⚠️ Ошибок доставки: {errors_total}"
                         f"  (↓{failed_in} ↑{failed_out})")
        else:
            lines.append("  Ошибок: 0")

        media_stats = await self._repo.count_pending_media()
        pending_media = int(media_stats.get("pending_count") or 0)
        media_retry_line = f"  ⏳ Медиа retry: {pending_media}"
        if pending_media:
            oldest = media_stats.get("oldest_created_at")
            if oldest:
                media_retry_line += (
                    f", старейшее "
                    f"{self._format_duration_compact(int(time.time()) - int(oldest))}"
                )
        lines.append(media_retry_line)

        empty_stats_getter = getattr(self._max, "get_pending_empty_recovery_stats", None)
        if callable(empty_stats_getter):
            empty_stats = empty_stats_getter()
            pending_empty = int(empty_stats.get("pending_count") or 0)
            empty_line = f"  ⏳ Empty/voice recovery: {pending_empty}"
            oldest_empty = empty_stats.get("oldest_created_at")
            if pending_empty and oldest_empty:
                empty_line += (
                    f", старейшее "
                    f"{self._format_duration_compact(int(time.time()) - int(oldest_empty))}"
                )
            lines.append(empty_line)

        if chat_activity:
            lines += ["", "💬 Активные чаты"]
            for c in chat_activity:
                title = (c["title"] or "—")[:30]
                lines.append(f"  {title:<32} ↓{c['inbound']}  ↑{c['outbound']}")

        lines += [
            "",
            f"🗂 Всего чатов: {total_chats}  (активных: {active_chats})",
        ]

        recovery_status_lines = await self._build_recovery_status_summary()
        if recovery_status_lines:
            lines += ["", *recovery_status_lines]

        if self._health is not None:
            snapshot = await self._health.get_snapshot()
            lines += ["", *render_health_summary(snapshot)]
        elif not self._max.is_ready() and max_issue is not None:
            lines += [
                "",
                "⚠️ Проблема MAX",
                f"  {max_issue.summary}",
            ]
            if getattr(max_issue, "requires_reauth", False):
                lines.append("  Требуется: reauth по SMS")

        if last_connected_at:
            lines.append(
                f"Последний успешный MAX connect: "
                f"{format_timestamp(last_connected_at)}"
            )

        return "\n".join(lines)

    async def _build_chats_message(self, period_hours: int = 24) -> str:
        """Список чатов с topic_id, режимом и активностью за period_hours часов."""
        bindings = await self._repo.list_bindings()
        if not bindings:
            return "🗂 Чаты: 0"

        since = int(time.time()) - period_hours * 3600
        activity = await self._repo.get_chat_activity_map_since(since)

        mode_badge = {
            "active": "✅",
            "readonly": "🔒",
            "disabled": "⏸",
        }

        def sort_key(binding: ChatBinding) -> tuple[int, str]:
            stats = activity.get(binding.max_chat_id, {})
            return (int(stats.get("total", 0)), binding.title.lower())

        ordered = sorted(bindings, key=sort_key, reverse=True)
        total_chats = len(bindings)
        active_chats = sum(1 for b in bindings if b.mode == "active")

        lines = [
            f"🗂 Чаты: {total_chats} (активных: {active_chats})",
            f"Активность за {period_hours}ч:",
        ]

        max_rows = 40
        for index, binding in enumerate(ordered):
            if index >= max_rows:
                lines.append(f"... и ещё {total_chats - max_rows}")
                break
            stats = activity.get(binding.max_chat_id, {})
            inbound = int(stats.get("inbound", 0))
            outbound = int(stats.get("outbound", 0))
            badge = mode_badge.get(binding.mode, "•")
            title = (binding.title or f"Чат {binding.max_chat_id}").strip()
            lines.append(
                f"{badge} #{binding.tg_topic_id} {title[:42]} · ↓{inbound} ↑{outbound}"
            )

        return "\n".join(lines)

    async def _build_help_message(self) -> str:
        """Справка по командам bridge."""
        return (
            "ℹ️ MAX Bridge — пересылка чатов MAX ↔ Telegram\n"
            "\n"
            "Каждый MAX-чат = отдельный топик в этой группе.\n"
            "Reply в топике = ответ обратно в MAX.\n"
            "\n"
            "📋 Команды (только для владельца):\n"
            "  /status — состояние bridge, статистика за 4ч\n"
            "  /chats  — список чатов с активностью за 24ч\n"
            "  /help   — эта справка\n"
            "\n"
            "📩 Команда для всех участников группы (в топике General):\n"
            "  /dm Имя Фамилия текст — начать новый DM в MAX\n"
            "\n"
            "💡 Пример /dm (пишите в General):\n"
            "  /dm Татьяна Геннадиевна Ладина Добрый день!"
        )

    async def _schedule_recovery_scan_after_connect(self):
        if self._recovery_scan_task is not None and not self._recovery_scan_task.done():
            return
        self._recovery_scan_task = asyncio.create_task(
            self._run_recovery_scan_after_connect(),
            name="recovery_snapshot_after_connect",
        )

    async def _run_recovery_scan_after_connect(self):
        await recovery_orchestrator.run_after_connect(
            safe_scan=self._safe_recovery_scan,
            log_scan_failure=self._log_recovery_scan_failure,
        )

    def _message_has_recovery_control_event(self, msg: MaxMessage) -> bool:
        return recovery_orchestrator.message_has_control_event(msg)

    def _schedule_recovery_event_scan(self, reason: str):
        self._recovery_event_scan_task, self._recovery_event_scan_at = (
            recovery_orchestrator.schedule_event_scan(
                collect_snapshot=getattr(self._max, "collect_recovery_snapshot", None),
                reason=reason,
                cooldowns=self._recovery_event_scan_cooldowns,
                delays=self._recovery_event_scan_delays,
                last_scan_at=self._recovery_event_last_scan_at,
                scan_reasons=self._recovery_event_scan_reasons,
                current_task=self._recovery_event_scan_task,
                current_scan_at=self._recovery_event_scan_at,
                run_scheduled_scan=self._run_scheduled_recovery_event_scan,
            )
        )

    def _log_recovery_scan_failure(self, *, reason: str, error: Exception):
        error_type = type(error).__name__
        logger.warning("recovery snapshot failed: %s", error_type)
        log_event(
            logger,
            logging.WARNING,
            "bridge.recovery.scan_finished",
            stage="recovery",
            outcome="failed",
            reason=reason,
            error_type=error_type,
        )

    async def _run_scheduled_recovery_event_scan(self, delay_seconds: float):
        await recovery_orchestrator.run_scheduled_event_scan(
            delay_seconds=delay_seconds,
            scan_reasons=self._recovery_event_scan_reasons,
            last_scan_at=self._recovery_event_last_scan_at,
            clear_scan_at=self._clear_recovery_event_scan_at,
            safe_scan=self._safe_recovery_scan,
            log_scan_failure=self._log_recovery_scan_failure,
        )

    def _clear_recovery_event_scan_at(self):
        self._recovery_event_scan_at = None

    async def _safe_recovery_scan(self, *, reason: str = "manual", notify: bool = False) -> dict[str, object]:
        return await recovery_orchestrator.safe_scan(
            max_adapter=self._max,
            repo=self._repo,
            scan_lock=self._recovery_scan_lock,
            reason=reason,
            notify=notify,
            maybe_notify=self._maybe_notify_recovery_changes,
        )

    async def _maybe_notify_recovery_changes(self, *, reason: str, result: dict[str, object]):
        notification_state = await recovery_reporter.maybe_notify_changes(
            reason=reason,
            result=result,
            last_digest=self._last_recovery_notification_digest,
            last_notified_at=self._last_recovery_notification_at,
            send_ops_notification=self._send_ops_notification,
        )
        if notification_state is not None:
            self._last_recovery_notification_digest, self._last_recovery_notification_at = notification_state

    async def run_weekly_recovery_snapshot(self, interval_seconds: int = 7 * 24 * 3600):
        """Periodic recovery registry refresh. Default cadence: weekly."""
        await bridge_background.run_weekly_recovery_snapshot(
            safe_scan=self._safe_recovery_scan,
            health=self._health,
            log_scan_failure=self._log_recovery_scan_failure,
            interval_seconds=interval_seconds,
        )

    def _format_recovery_freshness(self, last_scan_at: Optional[int]) -> str:
        return recovery_reporter.format_freshness(
            last_scan_at,
            format_duration_compact=self._format_duration_compact,
        )

    async def _build_recovery_status_summary(self) -> list[str]:
        try:
            return await recovery_reporter.build_status_summary(
                repo=self._repo,
                format_freshness_fn=self._format_recovery_freshness,
            )
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "bridge.recovery.status_summary_failed",
                stage="recovery",
                outcome="failed",
                error_type=type(e).__name__,
            )
            return []

    async def _build_recovery_report_message(self) -> str:
        return await recovery_reporter.build_report_message(
            repo=self._repo,
            format_freshness_fn=self._format_recovery_freshness,
        )

    def _parse_recovery_set_fields(self, entry, tokens: list[str]) -> dict[str, object]:
        return recovery_reporter.parse_set_fields(entry, tokens)

    async def _cmd_recovery(self, args: str) -> str:
        """Owner-only MAX account migration recovery commands."""
        return await bridge_recovery_command.handle_recovery(
            args=args,
            cfg=self._cfg,
            repo=self._repo,
            tg=self._tg,
            safe_scan=self._safe_recovery_scan,
            log_scan_failure=self._log_recovery_scan_failure,
            build_report=self._build_recovery_report_message,
            format_freshness=self._format_recovery_freshness,
            parse_set_fields=self._parse_recovery_set_fields,
        )

    async def _cmd_dm(self, args: str) -> str:
        """Инициировать новый DM в MAX по имени пользователя.

        Формат: /dm Имя Фамилия текст сообщения
        Bridge ищет пользователя в contacts и dialogs кеше pymax.
        Топик в Telegram создаётся автоматически из echo-сообщения.
        """
        return await bridge_dm_command.handle_dm(self._repo, self._max, args)

    async def run_periodic_status(self, interval_hours: int = 4):
        """Автоматически отправлять статусный отчёт каждые interval_hours часов."""
        await bridge_background.run_periodic_status(
            health=self._health,
            build_status_message=self._build_status_message,
            send_ops_notification=self._send_ops_notification,
            interval_hours=interval_hours,
        )

    # ── Startup tasks ─────────────────────────────────────────────────────

    async def fix_fallback_titles(self):
        await bridge_topics.fix_fallback_titles(
            repo=self._repo,
            tg=self._tg,
            max_adapter=self._max,
            schedule_recovery_scan=self._schedule_recovery_event_scan,
        )

    # ── MAX watchdog ──────────────────────────────────────────────────────

    async def run_max_watchdog(self,
                               alert_after_seconds: int = 60,
                               check_interval: int = 10):
        """Фоновая задача: следит за доступностью MAX.

        Если MAX недоступен дольше alert_after_seconds — отправляет уведомление
        владельцу. Повторное уведомление — только после восстановления и новой потери.
        """
        await bridge_background.run_max_watchdog(
            max_adapter=self._max,
            health=self._health,
            send_ops_notification=self._send_ops_notification,
            emit_health_alert=self._emit_health_alert,
            alert_after_seconds=alert_after_seconds,
            check_interval=check_interval,
        )

    # ── Cleanup ───────────────────────────────────────────────────────────

    async def run_cleanup(self):
        """Периодическая очистка старых записей. Запускать в фоне."""
        await bridge_background.run_cleanup(
            cfg=self._cfg,
            repo=self._repo,
            health=self._health,
        )
