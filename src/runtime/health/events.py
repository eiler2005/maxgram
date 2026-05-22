"""Health event log helpers."""

from pathlib import Path
from typing import Any

from .writer import append_jsonl


def append_event(path: Path, payload: dict[str, Any]):
    append_jsonl(path, payload)
