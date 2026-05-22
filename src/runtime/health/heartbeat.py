"""Runtime heartbeat helpers."""

import json
from pathlib import Path

from .state import HealthSnapshot, _now_ts


def heartbeat_payload(snapshot: HealthSnapshot) -> dict[str, int | str]:
    return {
        "ts": _now_ts(),
        "overall_status": snapshot.overall_status,
        "worker_restart_count": snapshot.worker_restart_count,
    }


def heartbeat_is_fresh(path: str | Path, max_age_seconds: int) -> bool:
    heartbeat_path = Path(path)
    if not heartbeat_path.exists():
        return False

    try:
        raw = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        ts = int(raw.get("ts", 0))
    except Exception:
        return False

    return (_now_ts() - ts) <= max(1, int(max_age_seconds))
