"""pending_media_downloads repository."""

import time
from typing import Optional

from .base import BaseRepo
from ..types import PendingMediaDownload


class PendingMediaRepo(BaseRepo):
    def _pending_media_from_row(self, row) -> PendingMediaDownload:
        return PendingMediaDownload(**dict(row))

    async def find_active_pending_media(
        self,
        *,
        max_chat_id: str,
        max_msg_id: str,
        attachment_index: int,
        kind: str,
    ) -> Optional[PendingMediaDownload]:
        async with self._db.execute(
            """SELECT * FROM pending_media_downloads
               WHERE max_chat_id = ? AND max_msg_id = ?
                 AND attachment_index = ? AND kind = ?
                 AND status IN ('pending', 'retry', 'leased')
               LIMIT 1""",
            (max_chat_id, max_msg_id, attachment_index, kind),
        ) as cur:
            row = await cur.fetchone()
            return self._pending_media_from_row(row) if row else None

    async def find_active_pending_media_by_reference(
        self,
        *,
        media_chat_id: str,
        media_msg_id: str,
        attachment_index: int,
        kind: str,
        reference_kind: str,
        reference_id: str,
    ) -> Optional[PendingMediaDownload]:
        async with self._db.execute(
            """SELECT * FROM pending_media_downloads
               WHERE media_chat_id = ? AND media_msg_id = ?
                 AND attachment_index = ? AND kind = ?
                 AND reference_kind = ? AND reference_id = ?
                 AND status IN ('pending', 'retry', 'leased')
               ORDER BY id ASC
               LIMIT 1""",
            (
                media_chat_id,
                media_msg_id,
                attachment_index,
                kind,
                reference_kind,
                reference_id,
            ),
        ) as cur:
            row = await cur.fetchone()
            return self._pending_media_from_row(row) if row else None

    async def enqueue_pending_media(self, job: PendingMediaDownload) -> int:
        now = int(time.time())
        created_at = job.created_at or now
        updated_at = now
        next_attempt_at = job.next_attempt_at or now
        cursor = await self._db.execute(
            """INSERT INTO pending_media_downloads
               (max_chat_id, max_msg_id, tg_topic_id, attachment_index, kind,
                source_type, media_chat_id, media_msg_id, reference_kind,
                reference_id, filename, duration, width, height, status,
                attempts, created_at, updated_at, next_attempt_at, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(max_chat_id, max_msg_id, attachment_index, kind)
               DO UPDATE SET
                 tg_topic_id = excluded.tg_topic_id,
                 source_type = excluded.source_type,
                 media_chat_id = excluded.media_chat_id,
                 media_msg_id = excluded.media_msg_id,
                 reference_kind = excluded.reference_kind,
                 reference_id = excluded.reference_id,
                 filename = excluded.filename,
                 duration = excluded.duration,
                 width = excluded.width,
                 height = excluded.height,
                 status = excluded.status,
                 updated_at = excluded.updated_at,
                 next_attempt_at = MIN(pending_media_downloads.next_attempt_at, excluded.next_attempt_at),
                 last_error = excluded.last_error
               WHERE pending_media_downloads.status != 'delivered'""",
            (
                job.max_chat_id,
                job.max_msg_id,
                job.tg_topic_id,
                job.attachment_index,
                job.kind,
                job.source_type,
                job.media_chat_id,
                job.media_msg_id,
                job.reference_kind,
                job.reference_id,
                job.filename,
                job.duration,
                job.width,
                job.height,
                job.status,
                job.attempts,
                created_at,
                updated_at,
                next_attempt_at,
                job.last_error,
            ),
        )
        await self._db.commit()
        if cursor.lastrowid:
            return int(cursor.lastrowid)
        async with self._db.execute(
            """SELECT id FROM pending_media_downloads
               WHERE max_chat_id = ? AND max_msg_id = ?
                 AND attachment_index = ? AND kind = ?""",
            (job.max_chat_id, job.max_msg_id, job.attachment_index, job.kind),
        ) as cur:
            row = await cur.fetchone()
            return int(row["id"]) if row else 0

    async def get_due_pending_media(
        self,
        *,
        now: Optional[int] = None,
        limit: int = 5,
    ) -> list[PendingMediaDownload]:
        now = int(time.time()) if now is None else now
        async with self._db.execute(
            """SELECT * FROM pending_media_downloads
               WHERE status IN ('pending', 'retry', 'leased')
                 AND next_attempt_at <= ?
                 AND (lease_until IS NULL OR lease_until < ?)
               ORDER BY next_attempt_at ASC, id ASC
               LIMIT ?""",
            (now, now, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._pending_media_from_row(row) for row in rows]

    async def lease_pending_media(
        self,
        job_id: int,
        *,
        lease_until: int,
        now: Optional[int] = None,
    ) -> bool:
        now = int(time.time()) if now is None else now
        cursor = await self._db.execute(
            """UPDATE pending_media_downloads
               SET status = 'leased', lease_until = ?, updated_at = ?
               WHERE id = ?
                 AND status IN ('pending', 'retry', 'leased')
                 AND (lease_until IS NULL OR lease_until < ?)""",
            (lease_until, now, job_id, now),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def mark_pending_media_retry(
        self,
        job_id: int,
        *,
        error: str,
        next_attempt_at: int,
        now: Optional[int] = None,
    ):
        now = int(time.time()) if now is None else now
        await self._db.execute(
            """UPDATE pending_media_downloads
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

    async def mark_pending_media_delivered(
        self,
        job_id: int,
        *,
        tg_msg_id: int,
        now: Optional[int] = None,
    ):
        now = int(time.time()) if now is None else now
        await self._db.execute(
            """UPDATE pending_media_downloads
               SET status = 'delivered',
                   attempts = attempts + 1,
                   updated_at = ?,
                   last_attempt_at = ?,
                   lease_until = NULL,
                   delivered_tg_msg_id = ?,
                   delivered_at = ?,
                   last_error = NULL
               WHERE id = ?""",
            (now, now, tg_msg_id, now, job_id),
        )
        await self._db.commit()

    async def mark_pending_media_failed(
        self,
        job_id: int,
        *,
        error: str,
        now: Optional[int] = None,
    ):
        now = int(time.time()) if now is None else now
        await self._db.execute(
            """UPDATE pending_media_downloads
               SET status = 'failed',
                   attempts = attempts + 1,
                   updated_at = ?,
                   last_attempt_at = ?,
                   lease_until = NULL,
                   last_error = ?
               WHERE id = ?""",
            (now, now, error, job_id),
        )
        await self._db.commit()

    async def count_pending_media(self) -> dict[str, Optional[int]]:
        async with self._db.execute(
            """SELECT COUNT(*) AS pending_count, MIN(created_at) AS oldest_created_at
               FROM pending_media_downloads
               WHERE status IN ('pending', 'retry', 'leased')"""
        ) as cur:
            row = await cur.fetchone()
        return {
            "pending_count": int(row["pending_count"] or 0),
            "oldest_created_at": row["oldest_created_at"],
        }
