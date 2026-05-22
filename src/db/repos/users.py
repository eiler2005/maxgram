"""known_users repository."""

import time
from typing import Optional

from .base import BaseRepo


class UsersRepo(BaseRepo):
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
        """Найти user_id по имени (регистронезависимо, включая кириллицу)."""
        name_lower = display_name.strip().lower()
        async with self._db.execute(
            "SELECT max_user_id, display_name FROM known_users"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            if row["display_name"].lower() == name_lower:
                return row["max_user_id"]
        return None
