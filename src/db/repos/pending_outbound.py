"""pending_outbound_messages repository."""

import time
from typing import Optional

from .base import BaseRepo
from ..types import PendingOutboundMessage


class PendingOutboundRepo(BaseRepo):
    def _pending_outbound_from_row(self, row) -> PendingOutboundMessage:
        return PendingOutboundMessage(**dict(row))

    async def enqueue_pending_outbound(self, job: PendingOutboundMessage) -> int:
        now = int(time.time())
        created_at = job.created_at or now
        updated_at = now
        next_attempt_at = job.next_attempt_at or now
        cursor = await self._db.execute(
            """INSERT INTO pending_outbound_messages
               (tg_topic_id, tg_msg_id, max_chat_id, reply_to_max_id, text,
                status, attempts, next_attempt_at, last_error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tg_topic_id, tg_msg_id)
               DO UPDATE SET
                 max_chat_id = excluded.max_chat_id,
                 reply_to_max_id = excluded.reply_to_max_id,
                 text = excluded.text,
                 status = excluded.status,
                 attempts = MAX(pending_outbound_messages.attempts, excluded.attempts),
                 next_attempt_at = MIN(pending_outbound_messages.next_attempt_at,
                                       excluded.next_attempt_at),
                 last_error = excluded.last_error,
                 updated_at = excluded.updated_at,
                 lease_until = NULL
               WHERE pending_outbound_messages.status != 'delivered'""",
            (
                job.tg_topic_id,
                job.tg_msg_id,
                job.max_chat_id,
                job.reply_to_max_id,
                job.text,
                job.status,
                job.attempts,
                next_attempt_at,
                job.last_error,
                created_at,
                updated_at,
            ),
        )
        await self._db.commit()
        if cursor.lastrowid:
            return int(cursor.lastrowid)
        async with self._db.execute(
            """SELECT id FROM pending_outbound_messages
               WHERE tg_topic_id = ? AND tg_msg_id = ?""",
            (job.tg_topic_id, job.tg_msg_id),
        ) as cur:
            row = await cur.fetchone()
            return int(row["id"]) if row else 0

    async def get_due_pending_outbound(
        self,
        *,
        now: Optional[int] = None,
        limit: int = 5,
    ) -> list[PendingOutboundMessage]:
        now = int(time.time()) if now is None else now
        async with self._db.execute(
            """SELECT * FROM pending_outbound_messages
               WHERE status IN ('pending', 'retry', 'leased')
                 AND text IS NOT NULL
                 AND next_attempt_at <= ?
                 AND (lease_until IS NULL OR lease_until < ?)
               ORDER BY next_attempt_at ASC, id ASC
               LIMIT ?""",
            (now, now, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._pending_outbound_from_row(row) for row in rows]

    async def lease_pending_outbound(
        self,
        job_id: int,
        *,
        lease_until: int,
        now: Optional[int] = None,
    ) -> bool:
        now = int(time.time()) if now is None else now
        cursor = await self._db.execute(
            """UPDATE pending_outbound_messages
               SET status = 'leased', lease_until = ?, updated_at = ?
               WHERE id = ?
                 AND status IN ('pending', 'retry', 'leased')
                 AND text IS NOT NULL
                 AND (lease_until IS NULL OR lease_until < ?)""",
            (lease_until, now, job_id, now),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def mark_pending_outbound_retry(
        self,
        job_id: int,
        *,
        error: str,
        next_attempt_at: int,
        now: Optional[int] = None,
    ):
        now = int(time.time()) if now is None else now
        await self._db.execute(
            """UPDATE pending_outbound_messages
               SET status = 'retry',
                   attempts = attempts + 1,
                   updated_at = ?,
                   last_attempt_at = ?,
                   next_attempt_at = ?,
                   lease_until = NULL,
                   last_error = ?
               WHERE id = ?""",
            (now, now, next_attempt_at, error, job_id),
        )
        await self._db.commit()

    async def mark_pending_outbound_delivered(
        self,
        job_id: int,
        *,
        max_msg_id: str,
        now: Optional[int] = None,
    ):
        now = int(time.time()) if now is None else now
        await self._db.execute(
            """UPDATE pending_outbound_messages
               SET status = 'delivered',
                   attempts = attempts + 1,
                   text = NULL,
                   updated_at = ?,
                   last_attempt_at = ?,
                   lease_until = NULL,
                   delivered_max_msg_id = ?,
                   delivered_at = ?,
                   last_error = NULL
               WHERE id = ?""",
            (now, now, max_msg_id, now, job_id),
        )
        await self._db.commit()

    async def mark_pending_outbound_failed(
        self,
        job_id: int,
        *,
        error: str,
        now: Optional[int] = None,
    ):
        now = int(time.time()) if now is None else now
        await self._db.execute(
            """UPDATE pending_outbound_messages
               SET status = 'failed',
                   attempts = attempts + 1,
                   text = NULL,
                   updated_at = ?,
                   last_attempt_at = ?,
                   lease_until = NULL,
                   last_error = ?
               WHERE id = ?""",
            (now, now, error, job_id),
        )
        await self._db.commit()

    async def expire_pending_outbound(
        self,
        *,
        older_than_seconds: int,
        now: Optional[int] = None,
    ) -> int:
        now = int(time.time()) if now is None else now
        cutoff = now - older_than_seconds
        cursor = await self._db.execute(
            """UPDATE pending_outbound_messages
               SET status = 'failed',
                   text = NULL,
                   updated_at = ?,
                   lease_until = NULL,
                   last_error = 'expired'
               WHERE status IN ('pending', 'retry', 'leased')
                 AND created_at < ?""",
            (now, cutoff),
        )
        await self._db.commit()
        return cursor.rowcount

    async def count_pending_outbound(self) -> dict[str, Optional[int]]:
        async with self._db.execute(
            """SELECT COUNT(*) AS pending_count, MIN(created_at) AS oldest_created_at
               FROM pending_outbound_messages
               WHERE status IN ('pending', 'retry', 'leased')"""
        ) as cur:
            row = await cur.fetchone()
        return {
            "pending_count": int(row["pending_count"] or 0),
            "oldest_created_at": row["oldest_created_at"],
        }
