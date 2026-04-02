#!/usr/bin/env python3
"""
Быстрая smoke-проверка bridge по SQLite-метаданным.

Не трогает MAX/Telegram API напрямую и не требует секретов сверх доступа к bridge.db.
Полезен после ручной проверки на тестовых чатах:
  1. отправить сообщение MAX -> Telegram
  2. отправить сообщение Telegram -> MAX
  3. посмотреть свежие записи в БД за последние N минут
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-report по bridge.db")
    parser.add_argument("--db", default="data/bridge.db", help="Путь к bridge.db")
    parser.add_argument("--minutes", type=int, default=15, help="Окно поиска в минутах")
    parser.add_argument("--limit", type=int, default=20, help="Макс. записей на секцию")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 1

    cutoff = int(time.time()) - args.minutes * 60
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"== Bridge smoke report ==")
    print(f"db: {db_path}")
    print(f"window: last {args.minutes} minutes")
    print()

    print("== Recent chat bindings ==")
    rows = conn.execute(
        """
        SELECT max_chat_id, tg_topic_id, title, mode, created_at
        FROM chat_bindings
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for row in rows:
        print(dict(row))
    if not rows:
        print("(no bindings)")
    print()

    print("== Recent message_map rows ==")
    rows = conn.execute(
        """
        SELECT max_msg_id, max_chat_id, tg_msg_id, tg_topic_id, direction, created_at
        FROM message_map
        WHERE created_at >= ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (cutoff, args.limit),
    ).fetchall()
    for row in rows:
        print(dict(row))
    if not rows:
        print("(no recent message_map rows)")
    print()

    print("== Recent delivery_log rows ==")
    rows = conn.execute(
        """
        SELECT max_msg_id, max_chat_id, direction, status, error, attempts, created_at, last_attempt_at
        FROM delivery_log
        WHERE created_at >= ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (cutoff, args.limit),
    ).fetchall()
    for row in rows:
        print(dict(row))
    if not rows:
        print("(no recent delivery_log rows)")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
