"""External await timeout helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import TypeVar

from ..logging_utils import log_event

T = TypeVar("T")

DEFAULT_OPERATION_TIMEOUT_SECONDS = 30
MEDIA_TRANSFER_TIMEOUT_SECONDS = 120


async def with_timeout_or_none(
    awaitable: Awaitable[T],
    *,
    timeout_seconds: int | float,
    logger: logging.Logger,
    event: str,
    operation: str,
    **fields,
) -> T | None:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        payload = {
            "stage": "external_await",
            "outcome": "timeout",
            "reason": "timeout",
            "operation": operation,
            "timeout_seconds": timeout_seconds,
        }
        payload.update(fields)
        log_event(logger, logging.ERROR, event, **payload)
        return None
