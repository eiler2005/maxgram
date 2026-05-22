"""SQLite schema migration runner."""

from __future__ import annotations

import time
from dataclasses import dataclass

import aiosqlite

from .models import SCHEMA


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS = [
    Migration(1, "baseline_schema", SCHEMA),
]


async def apply_migrations(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        )
        """
    )
    async with db.execute("SELECT version FROM schema_migrations") as cur:
        applied = {int(row[0]) for row in await cur.fetchall()}

    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        await db.executescript(migration.sql)
        await db.execute(
            """
            INSERT INTO schema_migrations(version, name, applied_at)
            VALUES (?, ?, ?)
            """,
            (migration.version, migration.name, int(time.time())),
        )
    await db.commit()
