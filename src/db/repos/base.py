"""Shared repository base."""

from collections.abc import Callable

import aiosqlite


class BaseRepo:
    def __init__(self, get_db: Callable[[], aiosqlite.Connection | None]):
        self._get_db = get_db

    @property
    def _db(self) -> aiosqlite.Connection:
        db = self._get_db()
        if db is None:
            raise RuntimeError("Repository is not connected")
        return db
