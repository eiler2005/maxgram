"""Stateful recovery scan scheduling and reporting boundary."""

import asyncio
import logging

from . import orchestrator as recovery_orchestrator
from . import reporter as recovery_reporter
from ...config.loader import AppConfig
from ...db.repository import Repository
from ...logging_utils import log_event
from ...runtime.health import RuntimeHealthStore
from ..contracts import MaxBridgePort, MaxMessage, TelegramBridgePort

logger = logging.getLogger("src.bridge.core")

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


class RecoveryScheduler:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        max_adapter: MaxBridgePort,
        tg: TelegramBridgePort,
        repo: Repository,
        health: RuntimeHealthStore | None,
        send_ops_notification,
        format_duration_compact,
    ):
        self._cfg = cfg
        self._max = max_adapter
        self._tg = tg
        self._repo = repo
        self._health = health
        self._send_ops_notification = send_ops_notification
        self._format_duration_compact = format_duration_compact
        self._scan_task: asyncio.Task | None = None
        self._event_scan_task: asyncio.Task | None = None
        self._event_scan_at: float | None = None
        self._event_scan_reasons: set[str] = set()
        self._event_last_scan_at: dict[str, float] = {}
        self._event_scan_delays = dict(RECOVERY_EVENT_SCAN_DELAYS)
        self._event_scan_cooldowns = dict(RECOVERY_EVENT_SCAN_COOLDOWNS)
        self._scan_lock = asyncio.Lock()
        self._last_notification_digest: str | None = None
        self._last_notification_at = 0.0

    async def schedule_scan_after_connect(self):
        if self._scan_task is not None and not self._scan_task.done():
            return
        self._scan_task = asyncio.create_task(
            self._run_scan_after_connect(),
            name="recovery_snapshot_after_connect",
        )

    async def _run_scan_after_connect(self):
        await recovery_orchestrator.run_after_connect(
            safe_scan=self.safe_scan,
            log_scan_failure=self.log_scan_failure,
        )

    def message_has_control_event(self, msg: MaxMessage) -> bool:
        return recovery_orchestrator.message_has_control_event(msg)

    def schedule_event_scan(self, reason: str):
        self._event_scan_task, self._event_scan_at = recovery_orchestrator.schedule_event_scan(
            collect_snapshot=self._max.collect_recovery_snapshot,
            reason=reason,
            cooldowns=self._event_scan_cooldowns,
            delays=self._event_scan_delays,
            last_scan_at=self._event_last_scan_at,
            scan_reasons=self._event_scan_reasons,
            current_task=self._event_scan_task,
            current_scan_at=self._event_scan_at,
            run_scheduled_scan=self._run_scheduled_event_scan,
        )

    def log_scan_failure(self, *, reason: str, error: Exception):
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

    async def _run_scheduled_event_scan(self, delay_seconds: float):
        await recovery_orchestrator.run_scheduled_event_scan(
            delay_seconds=delay_seconds,
            scan_reasons=self._event_scan_reasons,
            last_scan_at=self._event_last_scan_at,
            clear_scan_at=self._clear_event_scan_at,
            safe_scan=self.safe_scan,
            log_scan_failure=self.log_scan_failure,
        )

    def _clear_event_scan_at(self):
        self._event_scan_at = None

    async def safe_scan(
        self,
        *,
        reason: str = "manual",
        notify: bool = False,
    ) -> dict[str, object]:
        return await recovery_orchestrator.safe_scan(
            max_adapter=self._max,
            repo=self._repo,
            scan_lock=self._scan_lock,
            reason=reason,
            notify=notify,
            maybe_notify=self.maybe_notify_changes,
        )

    async def maybe_notify_changes(self, *, reason: str, result: dict[str, object]):
        notification_state = await recovery_reporter.maybe_notify_changes(
            reason=reason,
            result=result,
            last_digest=self._last_notification_digest,
            last_notified_at=self._last_notification_at,
            send_ops_notification=self._send_ops_notification,
        )
        if notification_state is not None:
            self._last_notification_digest, self._last_notification_at = notification_state

    async def run_weekly_snapshot(self, interval_seconds: int = 7 * 24 * 3600):
        """Periodic recovery registry refresh. Default cadence: weekly."""
        from .. import background as bridge_background

        await bridge_background.run_weekly_recovery_snapshot(
            safe_scan=self.safe_scan,
            health=self._health,
            log_scan_failure=self.log_scan_failure,
            interval_seconds=interval_seconds,
        )

    def format_freshness(self, last_scan_at: int | None) -> str:
        return recovery_reporter.format_freshness(
            last_scan_at,
            format_duration_compact=self._format_duration_compact,
        )

    async def build_status_summary(self) -> list[str]:
        try:
            return await recovery_reporter.build_status_summary(
                repo=self._repo,
                format_freshness_fn=self.format_freshness,
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

    async def build_report_message(self) -> str:
        return await recovery_reporter.build_report_message(
            repo=self._repo,
            format_freshness_fn=self.format_freshness,
        )

    def parse_set_fields(self, entry, tokens: list[str]) -> dict[str, object]:
        return recovery_reporter.parse_set_fields(entry, tokens)

    async def handle_command(self, args: str) -> str:
        from ..commands import recovery as bridge_recovery_command

        return await bridge_recovery_command.handle_recovery(
            args=args,
            cfg=self._cfg,
            repo=self._repo,
            tg=self._tg,
            safe_scan=self.safe_scan,
            log_scan_failure=self.log_scan_failure,
            build_report=self.build_report_message,
            format_freshness=self.format_freshness,
            parse_set_fields=self.parse_set_fields,
        )
