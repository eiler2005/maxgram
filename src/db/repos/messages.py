"""message_map and tg_reply_map repository."""

import time
from typing import Optional

from .base import BaseRepo
from ..types import MessageRecord, TgReplyMapping


class MessagesRepo(BaseRepo):
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
        await self._commit()

    async def get_tg_msg_by_max(self, max_chat_id: str, max_msg_id: str) -> Optional[int]:
        """Найти tg_msg_id по MAX (chat_id, msg_id) — для реакций и редактирования."""
        async with self._db.execute(
            "SELECT tg_msg_id FROM message_map WHERE max_chat_id = ? AND max_msg_id = ? AND tg_msg_id IS NOT NULL",
            (max_chat_id, max_msg_id),
        ) as cur:
            row = await cur.fetchone()
            return int(row["tg_msg_id"]) if row else None

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

    async def get_tg_reply_mapping(self, tg_msg_id: int) -> Optional[TgReplyMapping]:
        """Найти полный MAX mapping по TG message id."""
        async with self._db.execute(
            "SELECT * FROM tg_reply_map WHERE tg_msg_id = ?", (tg_msg_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return TgReplyMapping(**dict(row))
        async with self._db.execute(
            """SELECT tg_msg_id, max_chat_id, max_msg_id, tg_topic_id,
                      'message_map' AS source, created_at
               FROM message_map WHERE tg_msg_id = ?""",
            (tg_msg_id,),
        ) as cur:
            row = await cur.fetchone()
            return TgReplyMapping(**dict(row)) if row else None

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
            await self._commit()
