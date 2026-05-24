#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters.max.backends.pymax.reauth import reauthorize_with_console
from src.adapters.max.network import build_max_egress_profile
from src.config.loader import load_config
from src.logging_utils import mask_phone


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh MAX PyMax session with interactive SMS/2FA auth."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to bridge config.yaml, defaults to ./config.yaml",
    )
    parser.add_argument(
        "--no-clear-session",
        action="store_true",
        help="Do not delete existing PyMax session rows before auth.",
    )
    return parser.parse_args()


async def run() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    egress = build_max_egress_profile(cfg.max.egress)

    print(
        "Starting MAX reauth for",
        mask_phone(cfg.max.phone),
        f"via {egress.name}/{egress.type}.",
    )
    print("Run this only while the bridge container is stopped.")

    await reauthorize_with_console(
        phone=cfg.max.phone,
        data_dir=str(cfg.storage.data_dir),
        session_name=cfg.max.session_filename,
        egress=egress,
        clear_session=not args.no_clear_session,
    )
    print("MAX reauth completed. Restart the bridge container now.")


if __name__ == "__main__":
    asyncio.run(run())
