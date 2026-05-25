"""chat_bindings repository."""

from typing import Optional

from .base import BaseRepo
from ..types import ChatBinding


class BindingsRepo(BaseRepo):
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
        await self._commit()

    async def update_mode(self, max_chat_id: str, mode: str):
        await self._db.execute(
            "UPDATE chat_bindings SET mode = ? WHERE max_chat_id = ?",
            (mode, max_chat_id),
        )
        await self._commit()

    async def update_title(self, max_chat_id: str, title: str):
        await self._db.execute(
            "UPDATE chat_bindings SET title = ? WHERE max_chat_id = ?",
            (title, max_chat_id),
        )
        await self._commit()

    async def remap_binding_by_topic(self, tg_topic_id: int, new_max_chat_id: str) -> Optional[ChatBinding]:
        binding = await self.get_binding_by_topic(tg_topic_id)
        if binding is None:
            return None

        existing_target = await self.get_binding(new_max_chat_id)
        if existing_target is not None and existing_target.tg_topic_id != tg_topic_id:
            raise ValueError(
                f"MAX chat {new_max_chat_id} is already bound to topic {existing_target.tg_topic_id}"
            )

        await self._db.execute(
            "UPDATE chat_bindings SET max_chat_id = ? WHERE tg_topic_id = ?",
            (new_max_chat_id, tg_topic_id),
        )
        await self._commit()
        return ChatBinding(
            max_chat_id=new_max_chat_id,
            tg_topic_id=binding.tg_topic_id,
            title=binding.title,
            mode=binding.mode,
            created_at=binding.created_at,
        )

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
