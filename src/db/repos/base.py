"""Shared repository base."""

from collections.abc import Callable

import aiosqlite


class BaseRepo:
    def __init__(
        self,
        get_db: Callable[[], aiosqlite.Connection | None],
        should_autocommit: Callable[[], bool] | None = None,
    ):
        self._get_db = get_db
        self._should_autocommit = should_autocommit or (lambda: True)

    @property
    def _db(self) -> aiosqlite.Connection:
        db = self._get_db()
        if db is None:
            raise RuntimeError("Repository is not connected")
        return db

    async def _commit(self):
        if self._should_autocommit():
            await self._db.commit()
