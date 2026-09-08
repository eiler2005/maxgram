"""Состояние watchdog между запусками: счётчики fail, dedup, последний push."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class WatchdogState:
    """Плоский JSON-файл. Хранит только служебные счётчики, без данных bridge."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
            return True
        except Exception:
            return False

    # --- счётчики подряд идущих сбоев (гистерезис) ---

    def fail_count(self, rule: str) -> int:
        return int(self._data.get("fail_count", {}).get(rule, 0))

    def bump_fail(self, rule: str) -> int:
        counts = self._data.setdefault("fail_count", {})
        counts[rule] = int(counts.get(rule, 0)) + 1
        return counts[rule]

    def clear_fail(self, rule: str) -> None:
        self._data.setdefault("fail_count", {}).pop(rule, None)

    # --- активные алерты (чтобы отправить recovery ровно один раз) ---

    def is_alerting(self, rule: str) -> bool:
        return bool(self._data.get("alerting", {}).get(rule))

    def set_alerting(self, rule: str, value: bool, *, now: int | None = None) -> None:
        alerting = self._data.setdefault("alerting", {})
        started = self._data.setdefault("alert_started_at", {})
        if value:
            alerting[rule] = True
            started.setdefault(rule, int(now or 0))
        else:
            alerting.pop(rule, None)
            started.pop(rule, None)

    def alert_started_at(self, rule: str) -> int:
        return int(self._data.get("alert_started_at", {}).get(rule, 0))

    def active_alerts(self) -> list[str]:
        return sorted(self._data.get("alerting", {}))

    # --- dedup ---

    def last_sent_at(self, key: str) -> int:
        return int(self._data.get("last_sent", {}).get(key, 0))

    def mark_sent(self, key: str, ts: int) -> None:
        self._data.setdefault("last_sent", {})[key] = int(ts)

    # --- произвольные значения (restart baseline, push, daily summary) ---

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)
