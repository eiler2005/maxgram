"""delivery_log, stats, and retention cleanup repository."""

import time
from typing import Optional

from .base import BaseRepo


class DeliveryRepo(BaseRepo):
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
        """Активность по каждому чату за период."""
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
