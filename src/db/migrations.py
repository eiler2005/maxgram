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
    Migration(
        2,
        "pending_outbound_messages",
        """
        CREATE TABLE IF NOT EXISTS pending_outbound_messages (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_topic_id          INTEGER NOT NULL,
            tg_msg_id            INTEGER NOT NULL,
            max_chat_id          TEXT NOT NULL,
            reply_to_max_id      TEXT,
            text                 TEXT,
            status               TEXT NOT NULL DEFAULT 'pending',
            attempts             INTEGER NOT NULL DEFAULT 0,
            next_attempt_at      INTEGER NOT NULL,
            last_error           TEXT,
            created_at           INTEGER NOT NULL,
            updated_at           INTEGER NOT NULL,
            last_attempt_at      INTEGER,
            lease_until          INTEGER,
            delivered_max_msg_id TEXT,
            delivered_at         INTEGER,
            UNIQUE(tg_topic_id, tg_msg_id)
        );

        CREATE INDEX IF NOT EXISTS idx_pending_outbound_status_due
          ON pending_outbound_messages(status, next_attempt_at, lease_until);
        CREATE INDEX IF NOT EXISTS idx_pending_outbound_created
          ON pending_outbound_messages(created_at);
        """,
    ),
    Migration(
        3,
        "pending_inbound_messages",
        """
        CREATE TABLE IF NOT EXISTS pending_inbound_messages (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            max_chat_id          TEXT NOT NULL,
            max_msg_id           TEXT NOT NULL,
            tg_topic_id          INTEGER NOT NULL,
            text                 TEXT,
            status               TEXT NOT NULL DEFAULT 'pending',
            attempts             INTEGER NOT NULL DEFAULT 0,
            next_attempt_at      INTEGER NOT NULL,
            last_error           TEXT,
            created_at           INTEGER NOT NULL,
            updated_at           INTEGER NOT NULL,
            last_attempt_at      INTEGER,
            lease_until          INTEGER,
            delivered_tg_msg_id  INTEGER,
            delivered_at         INTEGER,
            UNIQUE(max_chat_id, max_msg_id)
        );

        CREATE INDEX IF NOT EXISTS idx_pending_inbound_status_due
          ON pending_inbound_messages(status, next_attempt_at, lease_until);
        CREATE INDEX IF NOT EXISTS idx_pending_inbound_created
          ON pending_inbound_messages(created_at);
        """,
    ),
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
