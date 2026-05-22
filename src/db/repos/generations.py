"""max_account_generations repository."""

import time
from collections.abc import Awaitable, Callable
from typing import Optional

from .base import BaseRepo


LogRecoveryEvent = Callable[..., Awaitable[None]]


class GenerationsRepo(BaseRepo):
    def __init__(self, get_db, log_recovery_event: LogRecoveryEvent):
        super().__init__(get_db)
        self._log_recovery_event = log_recovery_event

    async def upsert_max_account_generation(
        self,
        *,
        max_user_id: str,
        masked_phone: Optional[str],
        session_fingerprint_hash: Optional[str],
    ) -> dict[str, object]:
        now = int(time.time())
        async with self._db.execute(
            "SELECT * FROM max_account_generations WHERE status = 'active' ORDER BY last_seen_at DESC LIMIT 1"
        ) as cur:
            active = await cur.fetchone()

        previous_max_user_id = active["max_user_id"] if active else None
        migration_required = bool(previous_max_user_id and previous_max_user_id != max_user_id)
        if migration_required:
            await self._db.execute(
                "UPDATE max_account_generations SET status = 'retired' WHERE status = 'active' AND max_user_id != ?",
                (max_user_id,),
            )

        await self._db.execute(
            """INSERT INTO max_account_generations
               (max_user_id, masked_phone, session_fingerprint_hash, status, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, 'active', ?, ?)
               ON CONFLICT(max_user_id) DO UPDATE SET
                 masked_phone = excluded.masked_phone,
                 session_fingerprint_hash = excluded.session_fingerprint_hash,
                 status = 'active',
                 last_seen_at = excluded.last_seen_at""",
            (max_user_id, masked_phone, session_fingerprint_hash, now, now),
        )
        await self._log_recovery_event(
            registry_key=None,
            tg_topic_id=None,
            event_type="account_migration_required" if migration_required else "account_seen",
            details={
                "max_user_id": max_user_id,
                "previous_max_user_id": previous_max_user_id,
            },
            commit=False,
        )
        await self._db.commit()
        return {
            "migration_required": migration_required,
            "previous_max_user_id": previous_max_user_id,
            "max_user_id": max_user_id,
        }
