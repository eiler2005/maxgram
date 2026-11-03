"""telegram_callback_actions repository."""

import secrets
import sqlite3
import time
from typing import Optional

from .base import BaseRepo
from ..types import TelegramCallbackActionRecord, _json_compact


class CallbackActionsRepo(BaseRepo):
    def _record_from_row(self, row) -> TelegramCallbackActionRecord:
        return TelegramCallbackActionRecord(**dict(row))

    async def create_callback_action(
        self,
        *,
        action_type: str,
        max_chat_id: str,
        max_msg_id: str,
        payload: dict[str, object],
        tg_topic_id: Optional[int] = None,
        tg_msg_id: Optional[int] = None,
        source_type: Optional[str] = None,
    ) -> str:
        now = int(time.time())
        payload_json = _json_compact(payload)
        for _ in range(5):
            action_id = secrets.token_urlsafe(6)
            try:
                await self._db.execute(
                    """INSERT INTO telegram_callback_actions
                       (id, action_type, max_chat_id, max_msg_id, tg_topic_id,
                        tg_msg_id, source_type, payload_json, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        action_id,
                        action_type,
                        max_chat_id,
                        max_msg_id,
                        tg_topic_id,
                        tg_msg_id,
                        source_type,
                        payload_json,
                        now,
                    ),
                )
                await self._commit()
                return action_id
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("failed to allocate Telegram callback action id")

    async def get_callback_action(self, action_id: str) -> TelegramCallbackActionRecord | None:
        async with self._db.execute(
            "SELECT * FROM telegram_callback_actions WHERE id = ?",
            (action_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._record_from_row(row) if row else None

    async def attach_callback_action_message(
        self,
        action_id: str,
        *,
        tg_msg_id: int,
    ) -> None:
        await self._db.execute(
            "UPDATE telegram_callback_actions SET tg_msg_id = ? WHERE id = ?",
            (tg_msg_id, action_id),
        )
        await self._commit()

    async def mark_callback_action_used(
        self,
        action_id: str,
        *,
        error: Optional[str] = None,
        now: Optional[int] = None,
        clear_payload: bool = False,
    ) -> None:
        now = int(time.time()) if now is None else now
        status = "failed" if error else "used"
        await self._db.execute(
            """UPDATE telegram_callback_actions
               SET status = ?, used_at = ?, last_error = ?,
                   payload_json = CASE WHEN ? THEN '{}' ELSE payload_json END
               WHERE id = ?""",
            (status, now, error, clear_payload, action_id),
        )
        await self._commit()

    async def claim_callback_action(self, action_id: str) -> bool:
        """Only one confirm/cancel request can consume a pending action."""
        cur = await self._db.execute(
            """UPDATE telegram_callback_actions SET status = 'processing'
               WHERE id = ? AND status = 'pending'""",
            (action_id,),
        )
        claimed = cur.rowcount == 1
        await cur.close()
        await self._commit()
        return claimed

    async def expire_join_requests(self, before: int) -> None:
        await self._db.execute(
            """UPDATE telegram_callback_actions
               SET status = 'failed', last_error = 'expired', payload_json = '{}', used_at = ?
               WHERE action_type = 'max_join_request' AND created_at <= ?
                 AND status IN ('pending', 'processing')""",
            (int(time.time()), before),
        )
        await self._commit()
