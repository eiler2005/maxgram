from __future__ import annotations

import logging
import time
from typing import Optional

from .. import constants as max_constants
from ....bridge.contracts import is_probable_client_cid
from ....logging_utils import log_event
from .parser import RawPayloadParser

logger = logging.getLogger("src.adapters.max_adapter")


class RawHistoryCache:
    def __init__(self, *, raw_history, parser: RawPayloadParser):
        self._raw_history = raw_history
        self._parser = parser

    def _cleanup_raw_unwrapped_state(self):
        now = time.monotonic()
        self._raw_history.raw_unwrapped_message_ids = {
            key: expires_at
            for key, expires_at in self._raw_history.raw_unwrapped_message_ids.items()
            if expires_at > now
        }
        self._raw_history.raw_processed_message_ids = {
            key: expires_at
            for key, expires_at in self._raw_history.raw_processed_message_ids.items()
            if expires_at > now
        }
        self._raw_history.raw_history_messages = {
            key: value
            for key, value in self._raw_history.raw_history_messages.items()
            if value[0] > now
        }
        self._raw_history.expected_raw_history_messages = {
            msg_id: value
            for msg_id, value in self._raw_history.expected_raw_history_messages.items()
            if value[1] > now
        }

    def _remember_expected_raw_history_message(self, chat_id: str, msg_id: str):
        self._cleanup_raw_unwrapped_state()
        self._raw_history.expected_raw_history_messages[str(msg_id)] = (
            str(chat_id),
            time.monotonic() + max_constants.get("MAX_RAW_HISTORY_EXPECTED_TTL_SECONDS"),
        )

    def _expected_raw_history_chat_id(self, msg_id: object) -> Optional[str]:
        if msg_id is None:
            return None
        self._cleanup_raw_unwrapped_state()
        expected = self._raw_history.expected_raw_history_messages.get(str(msg_id))
        if expected is None:
            return None
        return expected[0]

    def _mark_raw_unwrapped_message(self, chat_id: str, msg_id: str):
        self._cleanup_raw_unwrapped_state()
        self._raw_history.raw_unwrapped_message_ids[(str(chat_id), str(msg_id))] = (
            time.monotonic() + 30
        )

    def _consume_raw_unwrapped_message(self, chat_id: str, msg_id: str) -> bool:
        self._cleanup_raw_unwrapped_state()
        return (
            self._raw_history.raw_unwrapped_message_ids.pop((str(chat_id), str(msg_id)), None)
            is not None
        )

    def _mark_raw_processed_message(self, chat_id: str, msg_id: str):
        self._cleanup_raw_unwrapped_state()
        self._raw_history.raw_processed_message_ids[(str(chat_id), str(msg_id))] = (
            time.monotonic() + 30
        )

    def _is_raw_processed_message(self, chat_id: str, msg_id: str) -> bool:
        self._cleanup_raw_unwrapped_state()
        return (str(chat_id), str(msg_id)) in self._raw_history.raw_processed_message_ids

    def _cache_raw_history_payload(self, payload: dict) -> int:
        if not isinstance(payload, dict):
            return 0

        raw_messages = self._parser._payload_value(payload, "messages")
        if not isinstance(raw_messages, list):
            return 0

        self._cleanup_raw_unwrapped_state()
        outer_chat_id = self._parser._payload_value(payload, "chatId", "chat_id")
        cached = 0
        now = time.monotonic()

        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                continue
            message = self._parser._normalize_message_dict(raw_message)
            if not self._parser._message_dict_has_content(message):
                continue

            msg_id = self._parser._payload_value(message, "id", "messageId", "message_id", "msgId")
            chat_id = (
                self._parser._payload_value(message, "chatId", "chat_id")
                or outer_chat_id
                or self._expected_raw_history_chat_id(msg_id)
            )
            if chat_id is None or msg_id is None:
                continue
            if is_probable_client_cid(chat_id):
                continue

            message_obj = self._parser._message_object_from_dict(
                message,
                str(chat_id),
                prefer_raw=True,
            )
            self._raw_history.raw_history_messages[(str(chat_id), str(msg_id))] = (
                now + max_constants.get("MAX_RAW_HISTORY_CACHE_TTL_SECONDS"),
                message_obj,
            )
            cached += 1

        if len(self._raw_history.raw_history_messages) > max_constants.get("MAX_RAW_HISTORY_CACHE_SIZE"):
            newest = sorted(
                self._raw_history.raw_history_messages.items(),
                key=lambda item: item[1][0],
                reverse=True,
            )[:max_constants.get("MAX_RAW_HISTORY_CACHE_SIZE")]
            self._raw_history.raw_history_messages = dict(newest)

        return cached

    def _get_cached_raw_history_message(self, chat_id: str, msg_id: str):
        self._cleanup_raw_unwrapped_state()
        cached = self._raw_history.raw_history_messages.get((str(chat_id), str(msg_id)))
        if cached is None:
            return None
        _expires_at, message = cached
        return message


class RawHistoryFetcher:
    def __init__(
        self,
        *,
        connection,
        backend,
        parser: RawPayloadParser,
        cache: RawHistoryCache,
    ):
        self._connection = connection
        self._backend = backend
        self._parser = parser
        self._cache = cache

    async def _fetch_raw_history_payload(
        self,
        *,
        chat_id_int: int,
        from_time: int,
        forward: int,
        backward: int,
        flow_id: Optional[str] = None,
    ) -> Optional[dict]:
        client = self._connection.client
        if not client or getattr(client, "_send_and_wait", None) is None:
            return None
        try:
            payload = self._backend.fetch_history_payload(
                chat_id=chat_id_int,
                from_time=from_time,
                forward=forward,
                backward=backward,
            )
            data = await client._send_and_wait(
                opcode=self._backend.opcode("CHAT_HISTORY", 49),
                payload=payload,
                timeout=10,
            )
        except Exception as e:
            log_event(
                logger,
                logging.INFO,
                "max.raw.history_fetch",
                flow_id=flow_id,
                direction="inbound",
                stage="recover",
                outcome="failed",
                reason="raw_history_failed",
                max_chat_id=str(chat_id_int),
                error=str(e),
            )
            return None

        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return None
        cached = self._cache._cache_raw_history_payload(payload)
        log_event(
            logger,
            logging.INFO,
            "max.raw.history_fetch",
            flow_id=flow_id,
            direction="inbound",
            stage="recover",
            outcome="received",
            max_chat_id=str(chat_id_int),
            message_count=len(self._parser._raw_history_message_dicts(payload)),
            cached_count=cached,
        )
        return payload
