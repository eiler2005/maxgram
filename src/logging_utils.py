"""
Shared logging helpers for bridge event tracing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

EVENT_FIELD_ORDER = [
    "event",
    "flow_id",
    "direction",
    "stage",
    "outcome",
    "reason",
    "max_chat_id",
    "max_msg_id",
    "tg_topic_id",
    "tg_msg_id",
]

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_DIGIT_RUN_RE = re.compile(r"\d{6,}")


def _preview_limit() -> int:
    raw = os.environ.get("LOG_PREVIEW_CHARS", "120").strip()
    try:
        return max(20, int(raw))
    except ValueError:
        return 120


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _mask_digit_run(match: re.Match[str]) -> str:
    digits = match.group(0)
    if len(digits) <= 6:
        return digits
    return f"{digits[:2]}***{digits[-2:]}"


def sanitize_preview(value: str | None, *, limit: int | None = None) -> str | None:
    if value is None:
        return None

    preview = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    preview = _CONTROL_RE.sub(" ", preview)
    preview = re.sub(r"\s+", " ", preview).strip()
    preview = _DIGIT_RUN_RE.sub(_mask_digit_run, preview)

    max_chars = limit if limit is not None else _preview_limit()
    if len(preview) > max_chars:
        preview = f"{preview[:max_chars - 1]}…"
    return preview


def mask_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 5:
        return "***"
    return f"+{digits[:1]}***{digits[-2:]}"


def sanitize_path(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).name


def sanitize_url(url: str | None) -> str | None:
    if not url:
        return None

    parsed = urlparse(url)
    host = parsed.netloc or ""
    path = parsed.path or ""
    if not host and not path:
        return None

    filename = Path(path).name if path else ""
    if filename:
        return f"{host}/{filename}" if host else filename
    return f"{host}{path}" if host else path


def build_max_flow_id(chat_id: str | None, msg_id: str | None) -> str | None:
    if not chat_id or not msg_id:
        return None
    return f"mx:{chat_id}:{msg_id}"


def build_tg_flow_id(topic_id: int | None, tg_msg_id: int | None) -> str | None:
    if topic_id is None or tg_msg_id is None:
        return None
    return f"tg:{topic_id}:{tg_msg_id}"


def sanitize_for_log(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return sanitize_preview(value, limit=_preview_limit())
    if isinstance(value, Path):
        return value.name
    if isinstance(value, dict):
        return {str(key): sanitize_for_log(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_for_log(item) for item in value]
    return value


def _format_kv_value(value: Any, *, mode: str) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return _compact_json(sanitize_for_log(value))
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"

    text = str(value)
    if mode == "mixed":
        text = sanitize_preview(text, limit=max(_preview_limit(), len(text))) or ""

    if not text or any(ch.isspace() or ch in {'"', "="} for ch in text):
        return _compact_json(text)
    return text


def _ordered_event_items(fields: dict[str, Any]) -> list[tuple[str, Any]]:
    ordered: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for key in EVENT_FIELD_ORDER:
        if key in fields:
            ordered.append((key, fields[key]))
            seen.add(key)
    for key in sorted(fields):
        if key not in seen:
            ordered.append((key, fields[key]))
    return ordered


class EventFormatter(logging.Formatter):
    def __init__(self, *, fmt_mode: str = "mixed"):
        super().__init__()
        self._fmt_mode = fmt_mode if fmt_mode in {"text", "json", "mixed"} else "mixed"

    def _base_payload(self, record: logging.LogRecord) -> dict[str, Any]:
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event_fields = getattr(record, "event_fields", None)
        if isinstance(event_fields, dict):
            payload.update(event_fields)
        return payload

    def format(self, record: logging.LogRecord) -> str:
        payload = self._base_payload(record)
        if self._fmt_mode == "json":
            return _compact_json(payload)

        base = f"{payload['timestamp']} [{payload['level']}] {payload['logger']}: {payload['message']}"
        event_fields = getattr(record, "event_fields", None)
        if not isinstance(event_fields, dict):
            return base

        suffix = " ".join(
            f"{key}={_format_kv_value(value, mode=self._fmt_mode)}"
            for key, value in _ordered_event_items(event_fields)
            if value is not None
        )
        return f"{base} {suffix}".rstrip()


def log_event(
    logger_: logging.Logger,
    level: int,
    event: str,
    *,
    message: str | None = None,
    preview: str | None = None,
    **fields: Any,
) -> None:
    event_fields = {"event": event}
    for key, value in fields.items():
        if value is not None:
            event_fields[key] = value

    if preview is not None and level <= logging.DEBUG:
        event_fields["preview"] = sanitize_preview(preview)

    logger_.log(level, message or event, extra={"event_fields": event_fields})
