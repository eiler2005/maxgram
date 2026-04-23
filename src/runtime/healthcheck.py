"""Docker healthcheck: supervisor heartbeat freshness."""

from __future__ import annotations

import os
import sys

from ..config.loader import load_config
from .health import heartbeat_is_fresh


def main() -> int:
    config_path = os.environ.get("CONFIG_PATH", "config.yaml")
    cfg = load_config(config_path)
    max_age = max(5, int(cfg.health.heartbeat_interval_seconds) * 3)
    return 0 if heartbeat_is_fresh(cfg.storage.data_dir / "health_heartbeat.json", max_age) else 1


if __name__ == "__main__":
    raise SystemExit(main())
