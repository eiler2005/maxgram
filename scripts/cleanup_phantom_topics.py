#!/usr/bin/env python3
"""One-time cleanup for MAX raw cid phantom Telegram topics."""

import asyncio
import logging
import sys

from src.adapters.max_adapter import MaxAdapter
from src.adapters.tg_adapter import TelegramAdapter
from src.bridge.core import BridgeCore
from src.config.loader import load_config
from src.db.repository import Repository


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> int:
    cfg = load_config()
    repo = Repository(cfg.storage.db_path)
    await repo.connect()
    tg = TelegramAdapter(
        cfg.telegram.bot_token,
        cfg.telegram.owner_id,
        cfg.telegram.forum_group_id,
        tmp_dir=str(cfg.storage.tmp_dir),
        ops_topic_id=cfg.telegram.ops_topic_id,
    )
    await tg.setup()
    max_adapter = MaxAdapter(
        phone=cfg.max.phone,
        data_dir=str(cfg.storage.data_dir),
        session_name=cfg.max.session_filename,
        tmp_dir=str(cfg.storage.tmp_dir),
    )
    bridge = BridgeCore(cfg, repo, max_adapter, tg)
    try:
        stats = await bridge.cleanup_phantom_topics()
        print(stats)
        return 0
    finally:
        await tg.close()
        await repo.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
