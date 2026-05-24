#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
import time
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow reauth while bridge heartbeat looks fresh.",
    )
    parser.add_argument(
        "--confirm-clear-session",
        action="store_true",
        help="Confirm deleting saved PyMax session rows before SMS auth.",
    )
    return parser.parse_args()


def bridge_heartbeat_is_fresh(data_dir: Path, *, max_age_seconds: int = 120) -> bool:
    heartbeat = data_dir / "health_heartbeat.json"
    try:
        age = time.time() - heartbeat.stat().st_mtime
    except FileNotFoundError:
        return False
    return age < max_age_seconds


def snapshot_session_db(data_dir: Path, session_name: str) -> Path | None:
    session_path = data_dir / Path(session_name).name
    if not session_path.exists():
        return None

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = data_dir / f"{session_path.name}.before-reauth-{timestamp}"
    suffix = 0
    while target.exists():
        suffix += 1
        target = data_dir / f"{session_path.name}.before-reauth-{timestamp}-{suffix}"

    target.write_bytes(session_path.read_bytes())
    target.chmod(0o600)
    return target


async def run() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    egress = build_max_egress_profile(cfg.max.egress)
    data_dir = Path(cfg.storage.data_dir)

    if not args.no_clear_session and not args.confirm_clear_session:
        raise SystemExit(
            "Refusing to clear saved MAX session without --confirm-clear-session. "
            "Use --no-clear-session for diagnostics that must keep the token untouched."
        )

    if bridge_heartbeat_is_fresh(data_dir) and not args.force:
        raise SystemExit(
            "Refusing to run MAX reauth while bridge heartbeat is fresh. "
            "Stop bridge first, or pass --force only after confirming no second MAX client is running."
        )

    print(
        "Starting MAX reauth for",
        mask_phone(cfg.max.phone),
        f"via {egress.name}/{egress.type}.",
    )
    print("Run this only while the bridge container is stopped.")
    snapshot_path = snapshot_session_db(data_dir, cfg.max.session_filename)
    if snapshot_path is not None:
        print(f"Session snapshot saved: {snapshot_path}")

    await reauthorize_with_console(
        phone=cfg.max.phone,
        data_dir=str(data_dir),
        session_name=cfg.max.session_filename,
        egress=egress,
        clear_session=not args.no_clear_session,
    )
    print("MAX reauth completed. Restart the bridge container now.")


if __name__ == "__main__":
    asyncio.run(run())
