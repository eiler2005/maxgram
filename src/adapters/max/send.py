from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from .deps import SendDeps
from .types import PendingOutboundAck
from ...logging_utils import build_max_flow_id, log_event, sanitize_path

logger = logging.getLogger("src.adapters.max_adapter")


class MaxSendService:
    def __init__(self, deps: SendDeps):
        self._deps = deps

    @property
    def _backend(self):
        return self._deps.backend

    @property
    def _client(self):
        return self._deps.connection.client

    @property
    def _started(self):
        return self._deps.connection.started

    @property
    def _pending_outbound_acks(self):
        return self._deps.outbound.pending_outbound_acks

    async def send_message(self, chat_id: str, text: str,
                           reply_to_msg_id: Optional[str] = None,
                           media_path: Optional[str] = None,
                           media_type: Optional[str] = None,
                           flow_id: Optional[str] = None) -> Optional[str]:
        """Отправить сообщение в MAX чат (текст и/или медиа).

        media_type: "photo" | "video" | "audio" | "document"

        Возвращает:
          str  — real max_msg_id
          None — ошибка
        """
        # Ждём подключения до 15 секунд (на случай reconnect)
        self._deps.runtime._set_last_outbound_failure(None, attempts=0)
        if not self._started:
            log_event(
                logger,
                logging.ERROR,
                "max.outbound.failed",
                flow_id=flow_id,
                direction="outbound",
                stage="transport",
                outcome="failed",
                reason="not_connected",
                max_chat_id=chat_id,
                media_type=media_type,
            )
            for _ in range(3):
                await asyncio.sleep(5)
                if self._started:
                    break
            else:
                self._deps.runtime._set_last_outbound_failure(
                    "MAX adapter is not connected",
                    attempts=1,
                )
                return None

        if not self._client:
            self._deps.runtime._set_last_outbound_failure(
                "MAX client is not initialized",
                attempts=1,
            )
            return None

        normalized_text = self._deps.runtime._normalize_outbound_text(text)
        max_attempts = 3
        retry_delays = (1, 2)

        for attempt in range(1, max_attempts + 1):
            loop = asyncio.get_running_loop()
            pending = PendingOutboundAck(
                chat_id=str(chat_id),
                text=normalized_text,
                reply_to_msg_id=reply_to_msg_id,
                created_monotonic=time.monotonic(),
                future=loop.create_future(),
            )
            self._pending_outbound_acks.append(pending)
            log_event(
                logger,
                logging.INFO,
                "max.outbound.send",
                flow_id=flow_id,
                direction="outbound",
                stage="transport",
                outcome="started",
                max_chat_id=chat_id,
                media_type=media_type,
                has_text=bool(normalized_text),
                reply_to_max_id=reply_to_msg_id,
                filename=sanitize_path(media_path),
                attempt=attempt,
                max_attempts=max_attempts,
            )

            try:
                attachment = None
                if media_path and Path(media_path).exists():
                    if media_type == "photo":
                        attachment = self._backend.make_photo_attachment(media_path)
                    elif media_type == "video":
                        attachment = self._backend.make_video_attachment(media_path)
                    else:  # audio, document
                        attachment = self._backend.make_file_attachment(media_path)

                kwargs: dict = {"chat_id": int(chat_id), "text": text}
                if reply_to_msg_id:
                    kwargs["reply_to"] = int(reply_to_msg_id)
                if attachment is not None:
                    kwargs["attachment"] = attachment
                result = await self._client.send_message(**kwargs)
                msg_id = self._deps.runtime._extract_result_msg_id(result)
                if msg_id:
                    self._deps.runtime._remember_expected_outbound_id(chat_id, msg_id)
                    self._deps.runtime._set_last_outbound_failure(None, attempts=attempt)
                    log_event(
                        logger,
                        logging.INFO,
                        "max.outbound.sent",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="sent",
                        max_chat_id=chat_id,
                        max_msg_id=msg_id,
                        media_type=media_type,
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                    return msg_id

                if not normalized_text:
                    error = "MAX send returned no message id"
                    self._deps.runtime._set_last_outbound_failure(error, attempts=attempt)
                    log_event(
                        logger,
                        logging.ERROR,
                        "max.outbound.failed",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="failed",
                        reason="max_send_failed",
                        max_chat_id=chat_id,
                        media_type=media_type,
                        error=error,
                        attempts=attempt,
                    )
                    return None

                try:
                    echoed_id = await asyncio.wait_for(asyncio.shield(pending.future), timeout=10)
                    self._deps.runtime._set_last_outbound_failure(None, attempts=attempt)
                    log_event(
                        logger,
                        logging.INFO,
                        "max.outbound.sent",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="sent",
                        max_chat_id=chat_id,
                        max_msg_id=str(echoed_id),
                        media_type=media_type,
                        reason="echo_ack",
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                    return str(echoed_id)
                except asyncio.TimeoutError:
                    error = "MAX outbound ack timeout"
                    if attempt < max_attempts:
                        retry_in_seconds = retry_delays[attempt - 1]
                        log_event(
                            logger,
                            logging.WARNING,
                            "max.outbound.retry",
                            flow_id=flow_id,
                            direction="outbound",
                            stage="transport",
                            outcome="retry",
                            reason="ack_timeout",
                            max_chat_id=chat_id,
                            media_type=media_type,
                            error=error,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            retry_in_seconds=retry_in_seconds,
                        )
                        await asyncio.sleep(retry_in_seconds)
                        continue

                    self._deps.runtime._set_last_outbound_failure(error, attempts=attempt)
                    log_event(
                        logger,
                        logging.ERROR,
                        "max.outbound.failed",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="failed",
                        reason="ack_timeout",
                        max_chat_id=chat_id,
                        media_type=media_type,
                        error=error,
                        attempts=attempt,
                    )
                    return None
            except Exception as e:
                error = str(e)
                retryable = self._deps.runtime._is_retryable_send_error(e)
                if retryable and attempt < max_attempts:
                    retry_in_seconds = retry_delays[attempt - 1]
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.outbound.retry",
                        flow_id=flow_id,
                        direction="outbound",
                        stage="transport",
                        outcome="retry",
                        reason="transport_error",
                        max_chat_id=chat_id,
                        media_type=media_type,
                        error=error,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        retry_in_seconds=retry_in_seconds,
                    )
                    await asyncio.sleep(retry_in_seconds)
                    continue

                self._deps.runtime._set_last_outbound_failure(error, attempts=attempt)
                log_event(
                    logger,
                    logging.ERROR,
                    "max.outbound.failed",
                    flow_id=flow_id,
                    direction="outbound",
                    stage="transport",
                    outcome="failed",
                    reason="max_send_failed",
                    max_chat_id=chat_id,
                    media_type=media_type,
                    error=error,
                    attempts=attempt,
                    retryable=retryable,
                )
                return None
            finally:
                if pending in self._pending_outbound_acks:
                    self._pending_outbound_acks.remove(pending)
                if not pending.future.done():
                    pending.future.cancel()

        return None
