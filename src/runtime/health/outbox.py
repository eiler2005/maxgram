"""Persistent alert notification outbox."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .state import _now_ts


@dataclass
class OutboxMessage:
    id: str
    text: str
    chat_id: int
    message_thread_id: Optional[int]
    label: str
    category: str
    created_at: int
    attempts: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "chat_id": self.chat_id,
            "message_thread_id": self.message_thread_id,
            "label": self.label,
            "category": self.category,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OutboxMessage":
        return cls(
            id=str(raw.get("id", uuid.uuid4().hex)),
            text=str(raw.get("text", "")),
            chat_id=int(raw["chat_id"]),
            message_thread_id=(
                int(raw["message_thread_id"]) if raw.get("message_thread_id") is not None else None
            ),
            label=str(raw.get("label", "unknown")),
            category=str(raw.get("category", "system")),
            created_at=int(raw.get("created_at", _now_ts())),
            attempts=int(raw.get("attempts", 0)),
            last_error=str(raw.get("last_error", "")),
        )


class AlertOutboxStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = asyncio.Lock()

    async def load(self) -> list[OutboxMessage]:
        async with self._lock:
            return self._load_unlocked()

    async def queue(self, message: OutboxMessage):
        async with self._lock:
            items = self._load_unlocked()
            items.append(message)
            self._rewrite_unlocked(items)

    async def rewrite(self, messages: list[OutboxMessage]):
        async with self._lock:
            self._rewrite_unlocked(messages)

    async def size(self) -> int:
        async with self._lock:
            return len(self._load_unlocked())

    def _load_unlocked(self) -> list[OutboxMessage]:
        if not self._path.exists():
            return []

        messages: list[OutboxMessage] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(OutboxMessage.from_dict(json.loads(line)))
                except Exception:
                    continue
        return messages

    def _rewrite_unlocked(self, messages: list[OutboxMessage]):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for message in messages:
                fh.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
        tmp_path.replace(self._path)
