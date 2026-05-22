from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from . import constants as max_constants
from .deps import ExplicitMaxService
from ...bridge.contracts import (
    MAX_DM_SWEEP_BACKFILL_SECONDS,
    MaxAttachment,
    MaxMessage,
    is_probable_client_cid,
)
from ...logging_utils import build_max_flow_id, log_event

logger = logging.getLogger("src.adapters.max_adapter")


class MaxVoiceRecoveryService(ExplicitMaxService):
    async def _recover_empty_message_from_recent_history(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        flow_id: str,
    ):
        try:
            chat_id_int = int(chat_id)
            raw_msg_id_str = str(raw_msg_id)
        except (TypeError, ValueError):
            return None

        cached = self._get_cached_raw_history_message(chat_id, raw_msg_id_str)
        if cached is not None:
            recovered = self._prepare_empty_recovery_candidate(
                cached,
                chat_id=chat_id,
                chat_id_int=chat_id_int,
                raw_msg_id_str=raw_msg_id_str,
                flow_id=flow_id,
                reason="raw_history_cache_match",
            )
            if recovered is not None:
                return recovered

        if not self._client:
            return None

        self._remember_expected_raw_history_message(chat_id, raw_msg_id_str)
        history_from_time = int(time.time() * 1000) + 60_000
        raw_payload = await self._fetch_raw_history_payload(
            chat_id_int=chat_id_int,
            from_time=history_from_time,
            forward=0,
            backward=10,
            flow_id=flow_id,
        )
        if raw_payload is not None:
            raw_candidate = self._find_raw_history_message_dict(raw_payload, raw_msg_id_str)
            if raw_candidate is not None:
                recovered = self._prepare_empty_recovery_candidate(
                    raw_candidate,
                    chat_id=chat_id,
                    chat_id_int=chat_id_int,
                    raw_msg_id_str=raw_msg_id_str,
                    flow_id=flow_id,
                    reason="raw_recent_history_match",
                )
                if recovered is not None:
                    return recovered

        if getattr(self._client, "fetch_history", None) is None:
            return None

        try:
            messages = await self._client.fetch_history(
                chat_id_int,
                from_time=history_from_time,
                forward=0,
                backward=10,
            )
        except Exception as e:
            await asyncio.sleep(0.2)
            cached = self._get_cached_raw_history_message(chat_id, raw_msg_id_str)
            if cached is not None:
                recovered = self._prepare_empty_recovery_candidate(
                    cached,
                    chat_id=chat_id,
                    chat_id_int=chat_id_int,
                    raw_msg_id_str=raw_msg_id_str,
                    flow_id=flow_id,
                    reason="raw_history_cache_after_fetch_error",
                )
                if recovered is not None:
                    return recovered

            log_event(
                logger,
                logging.WARNING,
                "max.inbound.empty_recovery",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="failed",
                reason="recent_history_failed",
                max_chat_id=chat_id,
                max_msg_id=raw_msg_id_str,
                error=str(e),
            )
            return None

        for candidate in messages or []:
            if isinstance(candidate, dict):
                candidate_id = self._payload_value(
                    candidate,
                    "id",
                    "messageId",
                    "message_id",
                    "msgId",
                )
            else:
                candidate_id = getattr(candidate, "id", None)
            if str(candidate_id) != raw_msg_id_str:
                continue
            return self._prepare_empty_recovery_candidate(
                candidate,
                chat_id=chat_id,
                chat_id_int=chat_id_int,
                raw_msg_id_str=raw_msg_id_str,
                flow_id=flow_id,
                reason="recent_history_match",
            )

        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="miss",
            reason="recent_history_message_not_found",
            max_chat_id=chat_id,
            max_msg_id=raw_msg_id_str,
        )
        return None

    def get_pending_empty_recovery_stats(self) -> dict[str, Optional[int]]:
        if not self._pending_empty_recoveries:
            return {"pending_count": 0, "oldest_created_at": None}
        created_values = [
            int(job.get("created_at") or 0)
            for job in self._pending_empty_recoveries.values()
            if job.get("created_at")
        ]
        oldest = min(created_values) if created_values else None
        return {
            "pending_count": len(self._pending_empty_recoveries),
            "oldest_created_at": oldest,
        }

    def _history_message_time_seconds(self, message) -> Optional[int]:
        value = (
            self._payload_value(message, "time")
            if isinstance(message, dict)
            else getattr(message, "time", None)
        )
        try:
            ts = int(value)
        except (TypeError, ValueError):
            return None
        if ts > 10_000_000_000:
            return ts // 1000
        return ts

    def _pending_empty_recovery_ids_for_chat(self, chat_id: str) -> set[str]:
        return {
            str(job.get("raw_msg_id"))
            for job in self._pending_empty_recoveries.values()
            if str(job.get("chat_id")) == str(chat_id) and job.get("raw_msg_id") is not None
        }

    def _log_history_sweep_pending_diagnostic(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        reason: str,
        flow_id: Optional[str],
        message: Optional[dict] = None,
        message_count: Optional[int] = None,
    ):
        key = (str(chat_id), str(raw_msg_id), reason)
        now = time.monotonic()
        if self._history_sweep_diagnostic_log_until.get(key, 0) > now:
            return
        self._history_sweep_diagnostic_log_until[key] = (
            now + max_constants.get("MAX_HISTORY_SWEEP_DIAGNOSTIC_TTL_SECONDS")
        )
        fields: dict[str, object] = {
            "flow_id": flow_id,
            "direction": "inbound",
            "stage": "history_sweep",
            "outcome": "diagnostic",
            "reason": reason,
            "max_chat_id": str(chat_id),
            "max_msg_id": str(raw_msg_id),
            "message_count": message_count,
        }
        if isinstance(message, dict):
            fields.update(
                {
                    "message_type": str(
                        self._payload_value(message, "type", "_type") or ""
                    ) or None,
                    "message_fields": self._safe_field_paths(message),
                    "raw_attachment_types": self._raw_attachment_types_from_message_dict(
                        message
                    ),
                }
            )
        log_event(
            logger,
            logging.INFO,
            "max.history_sweep.pending_diagnostic",
            **fields,
        )

    async def replay_recent_history(
        self,
        chat_id: str,
        *,
        limit: int = 30,
        since_ts: Optional[int] = None,
        flow_id: Optional[str] = None,
    ) -> int:
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return 0
        if is_probable_client_cid(chat_id_int):
            return 0

        from_time = int(time.time() * 1000) + 60_000
        raw_payload = await self._fetch_raw_history_payload(
            chat_id_int=chat_id_int,
            from_time=from_time,
            forward=0,
            backward=max(1, int(limit)),
            flow_id=flow_id,
        )
        candidates: list[object] = []
        if raw_payload is not None:
            pending_ids = self._pending_empty_recovery_ids_for_chat(str(chat_id))
            seen_pending_ids: set[str] = set()
            raw_messages = self._raw_history_message_dicts(raw_payload)
            for message in raw_messages:
                raw_history_msg_id = self._payload_value(
                    message,
                    "id",
                    "messageId",
                    "message_id",
                    "msgId",
                )
                raw_history_msg_id_str = (
                    str(raw_history_msg_id) if raw_history_msg_id is not None else ""
                )
                if raw_history_msg_id_str in pending_ids:
                    seen_pending_ids.add(raw_history_msg_id_str)
                candidate_chat_id = (
                    self._payload_value(message, "chatId", "chat_id")
                    or chat_id
                )
                if is_probable_client_cid(candidate_chat_id):
                    candidate_chat_id = chat_id
                if not self._message_dict_has_content(message):
                    if raw_history_msg_id_str in pending_ids:
                        self._log_history_sweep_pending_diagnostic(
                            chat_id=str(chat_id),
                            raw_msg_id=raw_history_msg_id_str,
                            reason="pending_message_without_content",
                            flow_id=flow_id,
                            message=message,
                            message_count=len(raw_messages),
                        )
                    continue
                candidates.append(
                    self._message_object_from_dict(
                        message,
                        str(candidate_chat_id),
                        prefer_raw=True,
                    )
                )
            for pending_id in pending_ids - seen_pending_ids:
                self._log_history_sweep_pending_diagnostic(
                    chat_id=str(chat_id),
                    raw_msg_id=pending_id,
                    reason="pending_message_not_found",
                    flow_id=flow_id,
                    message_count=len(raw_messages),
                )
        elif self._client and getattr(self._client, "fetch_history", None):
            try:
                candidates = list(
                    await self._client.fetch_history(
                        chat_id_int,
                        from_time=from_time,
                        forward=0,
                        backward=max(1, int(limit)),
                    )
                    or []
                )
            except Exception as e:
                log_event(
                    logger,
                    logging.INFO,
                    "max.history_sweep.fetch_failed",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="history_sweep",
                    outcome="failed",
                    max_chat_id=str(chat_id),
                    error=str(e),
                )
                return 0

        def sort_key(candidate):
            return self._history_message_time_seconds(candidate) or 0

        replayed = 0
        for candidate in sorted(candidates, key=sort_key):
            candidate_ts = self._history_message_time_seconds(candidate)
            if since_ts is not None and candidate_ts is not None and candidate_ts < since_ts:
                continue
            candidate_chat_id = str(getattr(candidate, "chat_id", None) or chat_id)
            if is_probable_client_cid(candidate_chat_id):
                setattr(candidate, "chat_id", chat_id_int)
            if not self._message_object_has_content(candidate):
                continue
            await self._handle_raw_message(candidate)
            replayed += 1

        if replayed:
            log_event(
                logger,
                logging.INFO,
                "max.history_sweep.replayed",
                flow_id=flow_id,
                direction="inbound",
                stage="history_sweep",
                outcome="replayed",
                max_chat_id=str(chat_id),
                replayed_count=replayed,
            )
        return replayed

    def _pending_empty_recovery_path(self) -> Path:
        return Path(self._data_dir) / max_constants.get("MAX_EMPTY_RECOVERY_STATE_FILE")

    def _pending_empty_recovery_key(self, chat_id: str, raw_msg_id: str) -> str:
        return f"{chat_id}:{raw_msg_id}"

    def _load_pending_empty_recoveries(self):
        path = self._pending_empty_recovery_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.inbound.empty_recovery_state",
                stage="startup",
                outcome="failed",
                reason="load_failed",
                error=str(e),
            )
            return
        if not isinstance(data, list):
            return
        pending: dict[str, dict[str, object]] = {}
        now = int(time.time())
        for item in data:
            if not isinstance(item, dict):
                continue
            chat_id = item.get("chat_id")
            raw_msg_id = item.get("raw_msg_id")
            if chat_id is None or raw_msg_id is None:
                continue
            job = {
                "chat_id": str(chat_id),
                "raw_msg_id": str(raw_msg_id),
                "msg_id": str(item.get("msg_id") or raw_msg_id),
                "message_type": (
                    str(item["message_type"])
                    if item.get("message_type") is not None
                    else None
                ),
                "attempts": int(item.get("attempts") or 0),
                "created_at": int(item.get("created_at") or now),
                "updated_at": int(item.get("updated_at") or now),
                "next_attempt_at": min(
                    int(item.get("next_attempt_at") or now),
                    now + max_constants.get("MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS"),
                ),
                "last_error": (
                    str(item["last_error"])
                    if item.get("last_error") is not None
                    else None
                ),
            }
            pending[self._pending_empty_recovery_key(str(chat_id), str(raw_msg_id))] = job
        self._pending_empty_recoveries = pending

    def _save_pending_empty_recoveries(self):
        path = self._pending_empty_recovery_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            data = sorted(
                self._pending_empty_recoveries.values(),
                key=lambda item: (
                    int(item.get("next_attempt_at") or 0),
                    str(item.get("chat_id") or ""),
                    str(item.get("raw_msg_id") or ""),
                ),
            )
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.inbound.empty_recovery_state",
                stage="runtime",
                outcome="failed",
                reason="save_failed",
                error=str(e),
            )

    def _empty_recovery_retry_delay(self, attempts: int) -> int:
        exponent = max(0, min(12, attempts - 1))
        return min(
            max_constants.get("MAX_EMPTY_RECOVERY_RETRY_BASE_SECONDS") * (2 ** exponent),
            max_constants.get("MAX_EMPTY_RECOVERY_RETRY_MAX_SECONDS"),
        )

    def _remember_pending_empty_recovery(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        msg_id: str,
        message_type: Optional[str],
        flow_id: str,
    ):
        key = self._pending_empty_recovery_key(chat_id, raw_msg_id)
        now = int(time.time())
        existing = self._pending_empty_recoveries.get(key)
        if existing is None:
            self._pending_empty_recoveries[key] = {
                "chat_id": str(chat_id),
                "raw_msg_id": str(raw_msg_id),
                "msg_id": str(msg_id),
                "message_type": message_type,
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
                "next_attempt_at": now + max_constants.get("MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS"),
                "last_error": None,
            }
            self._save_pending_empty_recoveries()
            log_event(
                logger,
                logging.INFO,
                "max.inbound.empty_recovery",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="queued",
                reason="durable_history_retry",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                retry_in_seconds=max_constants.get("MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS"),
            )
            return

        existing["updated_at"] = now
        existing["message_type"] = message_type
        existing["msg_id"] = str(msg_id)
        self._save_pending_empty_recoveries()

    def _forget_pending_empty_recovery(
        self,
        chat_id: str,
        raw_msg_id: str,
        *,
        flow_id: Optional[str] = None,
        reason: str = "recovered",
    ):
        key = self._pending_empty_recovery_key(str(chat_id), str(raw_msg_id))
        if self._pending_empty_recoveries.pop(key, None) is None:
            return
        self._save_pending_empty_recoveries()
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="completed",
            reason=reason,
            max_chat_id=chat_id,
            max_msg_id=raw_msg_id,
        )

    def _start_pending_empty_recovery_worker(self):
        task = self._pending_empty_recovery_worker
        if task is not None and not task.done():
            return
        self._pending_empty_recovery_worker = asyncio.create_task(
            self._run_pending_empty_recoveries()
        )
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery_worker_started",
            stage="startup",
            outcome="started",
            poll_interval_seconds=max_constants.get("MAX_EMPTY_RECOVERY_RETRY_POLL_SECONDS"),
            pending_count=len(self._pending_empty_recoveries),
        )

    async def _run_pending_empty_recoveries(self):
        while True:
            try:
                now = int(time.time())
                due_jobs = [
                    dict(job)
                    for job in self._pending_empty_recoveries.values()
                    if int(job.get("next_attempt_at") or 0) <= now
                ]
                for job in due_jobs:
                    await self._attempt_pending_empty_recovery(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "max.inbound.empty_recovery_worker_failed",
                    stage="recover",
                    outcome="failed",
                    error=str(e),
                )
            await asyncio.sleep(max_constants.get("MAX_EMPTY_RECOVERY_RETRY_POLL_SECONDS"))

    async def _attempt_pending_empty_recovery(self, job: dict[str, object]):
        chat_id = str(job.get("chat_id") or "")
        raw_msg_id = str(job.get("raw_msg_id") or "")
        msg_id = str(job.get("msg_id") or raw_msg_id)
        if not chat_id or not raw_msg_id:
            return
        flow_id = build_max_flow_id(chat_id, msg_id)
        key = self._pending_empty_recovery_key(chat_id, raw_msg_id)
        current = self._pending_empty_recoveries.get(key)
        if current is None:
            return

        attempts = int(current.get("attempts") or 0) + 1
        current["attempts"] = attempts
        current["updated_at"] = int(time.time())
        self._save_pending_empty_recoveries()

        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="retry",
            reason="durable_history_retry",
            max_chat_id=chat_id,
            max_msg_id=msg_id,
            attempt=attempts,
        )

        recovered = await self._recover_empty_message_from_recent_history(
            chat_id=chat_id,
            raw_msg_id=raw_msg_id,
            flow_id=flow_id,
        )
        if recovered is not None:
            self._forget_pending_empty_recovery(
                chat_id,
                raw_msg_id,
                flow_id=flow_id,
                reason="durable_history_recovered",
            )
            await self._handle_raw_message(recovered)
            return

        delay = self._empty_recovery_retry_delay(attempts)
        current = self._pending_empty_recoveries.get(key)
        if current is None:
            return
        current["updated_at"] = int(time.time())
        current["next_attempt_at"] = int(time.time()) + delay
        current["last_error"] = "history_message_not_found_or_empty"
        self._save_pending_empty_recoveries()
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="retry_scheduled",
            reason="durable_history_retry",
            max_chat_id=chat_id,
            max_msg_id=msg_id,
            attempt=attempts,
            retry_in_seconds=delay,
        )

    def _schedule_empty_recovery_cache_wait(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        msg_id: str,
        message_type: Optional[str],
        flow_id: str,
    ) -> bool:
        key = (str(chat_id), str(raw_msg_id))
        existing = self._pending_empty_recovery_tasks.get(key)
        if existing is not None and not existing.done():
            return True

        self._remember_pending_empty_recovery(
            chat_id=chat_id,
            raw_msg_id=raw_msg_id,
            msg_id=msg_id,
            message_type=message_type,
            flow_id=flow_id,
        )
        task = asyncio.create_task(
            self._recover_empty_message_from_raw_history_cache_later(
                chat_id=str(chat_id),
                raw_msg_id=str(raw_msg_id),
                msg_id=str(msg_id),
                message_type=message_type,
                flow_id=flow_id,
            )
        )
        self._pending_empty_recovery_tasks[key] = task
        log_event(
            logger,
            logging.INFO,
            "max.inbound.empty_recovery",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="queued",
            reason="raw_history_cache_wait",
            max_chat_id=chat_id,
            max_msg_id=msg_id,
            wait_seconds=max_constants.get("MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS"),
        )
        return True

    async def _recover_empty_message_from_raw_history_cache_later(
        self,
        *,
        chat_id: str,
        raw_msg_id: str,
        msg_id: str,
        message_type: Optional[str],
        flow_id: str,
    ):
        key = (str(chat_id), str(raw_msg_id))
        deadline = time.monotonic() + max_constants.get("MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS")
        try:
            chat_id_int = int(chat_id)
            raw_msg_id_str = str(raw_msg_id)
        except (TypeError, ValueError):
            self._pending_empty_recovery_tasks.pop(key, None)
            return

        try:
            while time.monotonic() < deadline:
                cached = self._get_cached_raw_history_message(chat_id, raw_msg_id_str)
                if cached is not None:
                    recovered = self._prepare_empty_recovery_candidate(
                        cached,
                        chat_id=chat_id,
                        chat_id_int=chat_id_int,
                        raw_msg_id_str=raw_msg_id_str,
                        flow_id=flow_id,
                        reason="raw_history_cache_delayed_match",
                    )
                    if recovered is not None:
                        self._forget_pending_empty_recovery(
                            chat_id,
                            raw_msg_id,
                            flow_id=flow_id,
                            reason="raw_history_cache_delayed_match",
                        )
                        await self._handle_raw_message(recovered)
                        return

                await asyncio.sleep(max_constants.get("MAX_EMPTY_RECOVERY_CACHE_POLL_SECONDS"))

            log_event(
                logger,
                logging.INFO,
                "max.inbound.empty_recovery",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="miss",
                reason="raw_history_cache_wait_timeout",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                waited_seconds=max_constants.get("MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS"),
            )
            log_event(
                logger,
                logging.INFO,
                "max.inbound.skipped",
                flow_id=flow_id,
                direction="inbound",
                stage="normalize",
                outcome="skipped",
                reason="empty_event",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                message_type=message_type,
                has_reaction_info=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "max.inbound.empty_recovery",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="failed",
                reason="raw_history_cache_wait_failed",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                error=str(e),
            )
        finally:
            if self._pending_empty_recovery_tasks.get(key) is asyncio.current_task():
                self._pending_empty_recovery_tasks.pop(key, None)
