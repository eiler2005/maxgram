"""
Data access layer — все операции с SQLite.
Принципы:
  - Никаких JOIN-монстров, простые запросы
  - Контент сообщений не хранится
  - Все методы async
"""

import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite

from .models import SCHEMA


@dataclass
class ChatBinding:
    max_chat_id: str
    tg_topic_id: int
    title: str
    mode: str  # active | readonly | disabled
    created_at: int


@dataclass
class KnownUser:
    max_user_id: str
    display_name: str
    updated_at: int


@dataclass
class MessageRecord:
    max_msg_id: str
    max_chat_id: str
    tg_msg_id: Optional[int]
    tg_topic_id: Optional[int]
    direction: str  # inbound | outbound
    created_at: int


@dataclass
class PendingMediaDownload:
    max_chat_id: str
    max_msg_id: str
    tg_topic_id: int
    attachment_index: int
    kind: str
    source_type: Optional[str]
    media_chat_id: str
    media_msg_id: str
    reference_kind: str
    reference_id: str
    filename: Optional[str] = None
    duration: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    status: str = "pending"
    attempts: int = 0
    created_at: int = 0
    updated_at: int = 0
    next_attempt_at: int = 0
    last_attempt_at: Optional[int] = None
    lease_until: Optional[int] = None
    last_error: Optional[str] = None
    delivered_tg_msg_id: Optional[int] = None
    delivered_at: Optional[int] = None
    id: Optional[int] = None


class Repository:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # ── ChatBinding ────────────────────────────────────────────────────────

    async def get_binding(self, max_chat_id: str) -> Optional[ChatBinding]:
        async with self._db.execute(
            "SELECT * FROM chat_bindings WHERE max_chat_id = ?", (max_chat_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return ChatBinding(**dict(row))
        return None

    async def get_binding_by_topic(self, tg_topic_id: int) -> Optional[ChatBinding]:
        async with self._db.execute(
            "SELECT * FROM chat_bindings WHERE tg_topic_id = ?", (tg_topic_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return ChatBinding(**dict(row))
        return None

    async def save_binding(self, binding: ChatBinding):
        await self._db.execute(
            """INSERT INTO chat_bindings (max_chat_id, tg_topic_id, title, mode, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(max_chat_id) DO UPDATE SET
                 tg_topic_id = excluded.tg_topic_id,
                 title = excluded.title,
                 mode = excluded.mode""",
            (binding.max_chat_id, binding.tg_topic_id, binding.title,
             binding.mode, binding.created_at),
        )
        await self._db.commit()

    async def update_mode(self, max_chat_id: str, mode: str):
        await self._db.execute(
            "UPDATE chat_bindings SET mode = ? WHERE max_chat_id = ?",
            (mode, max_chat_id),
        )
        await self._db.commit()

    async def update_title(self, max_chat_id: str, title: str):
        await self._db.execute(
            "UPDATE chat_bindings SET title = ? WHERE max_chat_id = ?",
            (title, max_chat_id),
        )
        await self._db.commit()

    async def find_phantom_topic_bindings(self) -> list[ChatBinding]:
        """Timestamp-like fallback topics that duplicated a real chat delivery."""
        async with self._db.execute(
            """SELECT DISTINCT cb.*
               FROM chat_bindings cb
               JOIN message_map phantom
                 ON phantom.max_chat_id = cb.max_chat_id
                AND phantom.direction = 'inbound'
               JOIN message_map real
                 ON real.max_msg_id = phantom.max_msg_id
                AND real.max_chat_id != phantom.max_chat_id
                AND real.direction = 'inbound'
               WHERE cb.title LIKE 'Чат 1779%'
                 AND cb.max_chat_id LIKE '1779%'
               ORDER BY cb.created_at DESC"""
        ) as cur:
            rows = await cur.fetchall()
            return [ChatBinding(**dict(row)) for row in rows]

    async def list_bindings(self) -> list[ChatBinding]:
        async with self._db.execute("SELECT * FROM chat_bindings ORDER BY created_at") as cur:
            rows = await cur.fetchall()
            return [ChatBinding(**dict(r)) for r in rows]

    # ── MessageMap (дедупликация) ──────────────────────────────────────────

    async def is_duplicate(self, max_msg_id: str, max_chat_id: str) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM message_map WHERE max_msg_id = ? AND max_chat_id = ?",
            (max_msg_id, max_chat_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def save_message(self, record: MessageRecord):
        await self._db.execute(
            """INSERT INTO message_map
               (max_msg_id, max_chat_id, tg_msg_id, tg_topic_id, direction, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(max_msg_id, max_chat_id) DO UPDATE SET
                 tg_msg_id = COALESCE(excluded.tg_msg_id, message_map.tg_msg_id),
                 tg_topic_id = COALESCE(excluded.tg_topic_id, message_map.tg_topic_id),
                 direction = excluded.direction""",
            (record.max_msg_id, record.max_chat_id, record.tg_msg_id,
             record.tg_topic_id, record.direction, record.created_at),
        )
        if record.tg_msg_id is not None:
            await self.save_tg_reply_mapping(
                record.tg_msg_id,
                record.max_chat_id,
                record.max_msg_id,
                record.tg_topic_id,
                source="message_map",
                commit=False,
            )
        await self._db.commit()

    async def get_max_msg_id_by_tg(self, tg_msg_id: int) -> Optional[str]:
        """Найти max_msg_id по tg_msg_id — для reply routing."""
        async with self._db.execute(
            "SELECT max_msg_id FROM tg_reply_map WHERE tg_msg_id = ?", (tg_msg_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row["max_msg_id"]
        async with self._db.execute(
            "SELECT max_msg_id FROM message_map WHERE tg_msg_id = ?", (tg_msg_id,)
        ) as cur:
            row = await cur.fetchone()
            return row["max_msg_id"] if row else None

    async def save_tg_reply_mapping(
        self,
        tg_msg_id: int,
        max_chat_id: str,
        max_msg_id: str,
        tg_topic_id: Optional[int],
        *,
        source: str,
        commit: bool = True,
    ):
        now = int(time.time())
        await self._db.execute(
            """INSERT INTO tg_reply_map
               (tg_msg_id, max_chat_id, max_msg_id, tg_topic_id, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(tg_msg_id) DO UPDATE SET
                 max_chat_id = excluded.max_chat_id,
                 max_msg_id = excluded.max_msg_id,
                 tg_topic_id = excluded.tg_topic_id,
                 source = excluded.source""",
            (tg_msg_id, max_chat_id, max_msg_id, tg_topic_id, source, now),
        )
        if commit:
            await self._db.commit()

    # ── PendingMediaDownloads ──────────────────────────────────────────────

    def _pending_media_from_row(self, row) -> PendingMediaDownload:
        return PendingMediaDownload(**dict(row))

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

    # ── DeliveryLog ────────────────────────────────────────────────────────

    async def log_delivery(self, max_msg_id: str, max_chat_id: str,
                           direction: str, status: str, error: str = None,
                           attempts: int = 1):
        now = int(time.time())
        await self._db.execute(
            """INSERT INTO delivery_log
               (max_msg_id, max_chat_id, direction, status, error, attempts, created_at, last_attempt_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (max_msg_id, max_chat_id, direction, status, error, attempts, now, now),
        )
        await self._db.commit()

    async def get_failed_messages(self, limit: int = 50) -> list[dict]:
        async with self._db.execute(
            """SELECT * FROM delivery_log
               WHERE status = 'failed' AND attempts < 5
               ORDER BY last_attempt_at ASC LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ── Stats ─────────────────────────────────────────────────────────────

    async def count_messages_since(self, since_ts: int) -> dict[str, int]:
        """Количество сообщений по направлениям начиная с since_ts."""
        async with self._db.execute(
            """SELECT direction, COUNT(*) as cnt
               FROM message_map WHERE created_at >= ?
               GROUP BY direction""",
            (since_ts,),
        ) as cur:
            rows = await cur.fetchall()
        return {row["direction"]: row["cnt"] for row in rows}

    async def count_deliveries_since(self, since_ts: int) -> dict[str, int]:
        """Количество доставок по направлению+статусу начиная с since_ts."""
        async with self._db.execute(
            """SELECT direction, status, COUNT(*) as cnt
               FROM delivery_log WHERE created_at >= ?
               GROUP BY direction, status""",
            (since_ts,),
        ) as cur:
            rows = await cur.fetchall()
        result: dict[str, int] = {}
        for row in rows:
            key = f"{row['direction']}_{row['status']}"
            result[key] = row["cnt"]
        return result

    async def get_chat_activity_since(self, since_ts: int,
                                      limit: int = 10) -> list[dict]:
        """Топ-N активных чатов за период. Возвращает title, inbound, outbound."""
        async with self._db.execute(
            """SELECT cb.title,
                      SUM(CASE WHEN mm.direction='inbound'  THEN 1 ELSE 0 END) AS inbound,
                      SUM(CASE WHEN mm.direction='outbound' THEN 1 ELSE 0 END) AS outbound,
                      COUNT(mm.id) AS total
               FROM chat_bindings cb
               JOIN message_map mm
                 ON cb.max_chat_id = mm.max_chat_id AND mm.created_at >= ?
               GROUP BY cb.max_chat_id
               ORDER BY total DESC
               LIMIT ?""",
            (since_ts, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_chat_activity_map_since(self, since_ts: int) -> dict[str, dict[str, int]]:
        """Активность по каждому чату за период.

        Возвращает:
          {
            "<max_chat_id>": {"inbound": N, "outbound": M, "total": T},
            ...
          }
        """
        async with self._db.execute(
            """SELECT max_chat_id,
                      SUM(CASE WHEN direction='inbound'  THEN 1 ELSE 0 END) AS inbound,
                      SUM(CASE WHEN direction='outbound' THEN 1 ELSE 0 END) AS outbound,
                      COUNT(id) AS total
               FROM message_map
               WHERE created_at >= ?
               GROUP BY max_chat_id""",
            (since_ts,),
        ) as cur:
            rows = await cur.fetchall()

        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result[str(row["max_chat_id"])] = {
                "inbound": int(row["inbound"] or 0),
                "outbound": int(row["outbound"] or 0),
                "total": int(row["total"] or 0),
            }
        return result

    # ── Retention cleanup ─────────────────────────────────────────────────

    # ── KnownUsers ────────────────────────────────────────────────────────

    async def save_user(self, user_id: str, display_name: str):
        """Сохранить или обновить имя пользователя (upsert по user_id)."""
        now = int(time.time())
        await self._db.execute(
            """INSERT INTO known_users (max_user_id, display_name, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(max_user_id) DO UPDATE SET
                 display_name = excluded.display_name,
                 updated_at   = excluded.updated_at""",
            (user_id, display_name, now),
        )
        await self._db.commit()

    async def find_user_by_name(self, display_name: str) -> Optional[str]:
        """Найти user_id по имени (регистронезависимо, включая кириллицу).

        SQLite COLLATE NOCASE работает только для ASCII, поэтому сравниваем
        через Python .lower() после выборки кандидатов.
        """
        name_lower = display_name.strip().lower()
        async with self._db.execute(
            "SELECT max_user_id, display_name FROM known_users"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            if row["display_name"].lower() == name_lower:
                return row["max_user_id"]
        return None

    # ── Retention cleanup ─────────────────────────────────────────────────

    async def cleanup_old_messages(self, older_than_days: int):
        cutoff = int(time.time()) - older_than_days * 86400
        await self._db.execute(
            "DELETE FROM message_map WHERE created_at < ?", (cutoff,)
        )
        await self._db.commit()

    async def cleanup_old_logs(self, older_than_days: int):
        cutoff = int(time.time()) - older_than_days * 86400
        await self._db.execute(
            "DELETE FROM delivery_log WHERE created_at < ?", (cutoff,)
        )
        await self._db.commit()
