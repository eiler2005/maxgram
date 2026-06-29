"""delivered_media_parts repository."""

import time
from typing import Optional

from .base import BaseRepo
from ..types import DeliveredMediaPart


class DeliveredMediaRepo(BaseRepo):
    def _part_from_row(self, row) -> DeliveredMediaPart:
        return DeliveredMediaPart(**dict(row))

    async def save_delivered_media_part(
        self,
        *,
        max_chat_id: str,
        base_max_msg_id: str,
        attachment_index: int,
        kind: str,
        tg_msg_id: int,
        tg_topic_id: Optional[int],
        source: str,
        media_chat_id: Optional[str] = None,
        media_msg_id: Optional[str] = None,
        reference_kind: Optional[str] = None,
        reference_id: Optional[str] = None,
        commit: bool = True,
    ) -> None:
        now = int(time.time())
        await self._db.execute(
            """INSERT INTO delivered_media_parts
               (max_chat_id, base_max_msg_id, attachment_index, kind,
                tg_msg_id, tg_topic_id, source, media_chat_id, media_msg_id,
                reference_kind, reference_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(max_chat_id, base_max_msg_id, attachment_index, kind)
               DO UPDATE SET
                 tg_msg_id = COALESCE(delivered_media_parts.tg_msg_id, excluded.tg_msg_id),
                 tg_topic_id = COALESCE(delivered_media_parts.tg_topic_id, excluded.tg_topic_id),
                 source = COALESCE(delivered_media_parts.source, excluded.source),
                 media_chat_id = COALESCE(delivered_media_parts.media_chat_id, excluded.media_chat_id),
                 media_msg_id = COALESCE(delivered_media_parts.media_msg_id, excluded.media_msg_id),
                 reference_kind = COALESCE(delivered_media_parts.reference_kind, excluded.reference_kind),
                 reference_id = COALESCE(delivered_media_parts.reference_id, excluded.reference_id),
                 updated_at = excluded.updated_at""",
            (
                max_chat_id,
                base_max_msg_id,
                attachment_index,
                kind,
                tg_msg_id,
                tg_topic_id,
                source,
                media_chat_id,
                media_msg_id,
                reference_kind,
                reference_id,
                now,
                now,
            ),
        )
        if commit:
            await self._commit()

    async def find_delivered_media_part(
        self,
        *,
        max_chat_id: str,
        base_max_msg_id: str,
        attachment_index: int,
        kind: str,
    ) -> Optional[DeliveredMediaPart]:
        async with self._db.execute(
            """SELECT * FROM delivered_media_parts
               WHERE max_chat_id = ? AND base_max_msg_id = ?
                 AND attachment_index = ? AND kind = ?
               LIMIT 1""",
            (max_chat_id, base_max_msg_id, attachment_index, kind),
        ) as cur:
            row = await cur.fetchone()
        return self._part_from_row(row) if row else None

    async def find_delivered_media_part_by_reference(
        self,
        *,
        max_chat_id: str,
        base_max_msg_id: str,
        kind: str,
        reference_kind: str,
        reference_id: str,
    ) -> Optional[DeliveredMediaPart]:
        async with self._db.execute(
            """SELECT * FROM delivered_media_parts
               WHERE max_chat_id = ? AND base_max_msg_id = ? AND kind = ?
                 AND reference_kind = ? AND reference_id = ?
               ORDER BY id ASC
               LIMIT 1""",
            (max_chat_id, base_max_msg_id, kind, reference_kind, reference_id),
        ) as cur:
            row = await cur.fetchone()
        return self._part_from_row(row) if row else None

    async def has_delivered_media_parts(
        self,
        *,
        max_chat_id: str,
        base_max_msg_id: str,
    ) -> bool:
        async with self._db.execute(
            """SELECT 1 FROM delivered_media_parts
               WHERE max_chat_id = ? AND base_max_msg_id = ?
               LIMIT 1""",
            (max_chat_id, base_max_msg_id),
        ) as cur:
            row = await cur.fetchone()
        return row is not None
