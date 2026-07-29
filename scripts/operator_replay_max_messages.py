#!/usr/bin/env python3
"""One-shot operator replay for specific MAX messages.

The script is intentionally metadata-only: it accepts explicit message ids,
does not print message text/raw payloads, and uses the normal bridge pipeline
for media download, Telegram delivery, reply mappings, and delivery logging.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters.max_adapter import MaxAdapter
from src.adapters.tg_adapter import TelegramAdapter
from src.bridge.core import BridgeCore
from src.config.loader import load_config
from src.db.repository import Repository
from src.logging_utils import build_max_flow_id


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay exact MAX message ids through the bridge pipeline.",
    )
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--msg-id", action="append", required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--since-ts", type=int, default=None)
    parser.add_argument("--from-time-ms", type=int, default=None)
    parser.add_argument("--ready-timeout", type=float, default=60.0)
    parser.add_argument(
        "--exact-first",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Try exact MSG_GET recovery before recent history replay.",
    )
    parser.add_argument(
        "--mark-partial",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mark existing delivered rows as recoverable partial before replay.",
    )
    return parser.parse_args()


def _mark_delivery_rows_partial(db_path: str, chat_id: str, msg_ids: set[str]) -> int:
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE delivery_log
            SET status='partial',
                error='attachment_download_failed:forced_replay_unsupported',
                last_attempt_at=?
            WHERE direction='inbound'
              AND max_chat_id=?
              AND max_msg_id IN ({})
            """.format(",".join("?" for _ in msg_ids)),
            (now, chat_id, *sorted(msg_ids)),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


async def _wait_until_ready(max_adapter: MaxAdapter, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if max_adapter.is_ready():
            return
        await asyncio.sleep(0.5)
    raise TimeoutError("MAX adapter did not become ready")


async def _run() -> int:
    args = _parse_args()
    target_ids = {str(item) for item in args.msg_id}
    cfg = load_config(os.environ.get("CONFIG_PATH", "config.yaml"))

    marked_rows = (
        _mark_delivery_rows_partial(cfg.storage.db_path, args.chat_id, target_ids)
        if args.mark_partial
        else 0
    )

    repo = Repository(cfg.storage.db_path)
    max_adapter = MaxAdapter(
        phone=cfg.max.phone,
        data_dir=cfg.storage.session_path,
        session_name=cfg.max.session_filename,
        tmp_dir=str(cfg.storage.tmp_dir),
        egress_config=cfg.max.egress,
    )
    tg_adapter = TelegramAdapter(
        bot_token=cfg.telegram.bot_token,
        owner_id=cfg.telegram.owner_id,
        forum_group_id=cfg.telegram.forum_group_id,
        tmp_dir=str(cfg.storage.tmp_dir),
    )
    start_task: asyncio.Task | None = None
    try:
        await repo.connect()
        await tg_adapter.setup()
        BridgeCore(cfg, repo, max_adapter, tg_adapter, ops_notifier=tg_adapter)
        with contextlib.suppress(Exception):
            max_adapter._state.start_handlers.clear()
        start_task = asyncio.create_task(max_adapter.start())
        await _wait_until_ready(max_adapter, args.ready_timeout)

        async def is_known_message(chat_id: str, msg_id: str) -> bool:
            return str(chat_id) != str(args.chat_id) or str(msg_id) not in target_ids

        exact_replayed = 0
        if args.exact_first:
            for msg_id in sorted(target_ids):
                raw_message = await max_adapter._media._fetch_raw_message_payload_by_id(
                    chat_id=args.chat_id,
                    msg_id=msg_id,
                    flow_id=build_max_flow_id(args.chat_id, msg_id),
                )
                if raw_message is None:
                    continue
                candidate = max_adapter._raw_payload._message_object_from_dict(
                    raw_message,
                    args.chat_id,
                    prefer_raw=True,
                )
                await max_adapter._events._handle_raw_message(candidate)
                exact_replayed += 1

        ranged_replayed = 0
        if args.from_time_ms is not None:
            raw_payload = await max_adapter._raw_payload._fetch_raw_history_payload(
                chat_id_int=int(args.chat_id),
                from_time=args.from_time_ms,
                forward=0,
                backward=max(1, int(args.limit)),
                flow_id=build_max_flow_id(args.chat_id, "operator-replay-range"),
            )
            for raw_message in max_adapter._raw_payload._raw_history_message_dicts(
                raw_payload or {}
            ):
                msg_id = max_adapter._raw_payload._payload_value(
                    raw_message,
                    "id",
                    "messageId",
                    "message_id",
                    "msgId",
                )
                if str(msg_id or "") not in target_ids:
                    continue
                candidate = max_adapter._raw_payload._message_object_from_dict(
                    raw_message,
                    args.chat_id,
                    prefer_raw=True,
                )
                await max_adapter._events._handle_raw_message(candidate)
                ranged_replayed += 1

        history_replayed = await max_adapter.replay_recent_history(
            args.chat_id,
            limit=args.limit,
            since_ts=args.since_ts,
            flow_id=build_max_flow_id(args.chat_id, "operator-replay"),
            is_known_message=is_known_message,
        )
        print(
            "operator_replay_result "
            f"chat_id={args.chat_id} target_count={len(target_ids)} "
            f"marked_rows={marked_rows} exact_replayed={exact_replayed} "
            f"ranged_replayed={ranged_replayed} history_replayed={history_replayed}"
        )
        return 0 if exact_replayed or ranged_replayed or history_replayed else 2
    finally:
        await max_adapter.close()
        await tg_adapter.close()
        await repo.close()
        if start_task is not None:
            start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await start_task


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
