"""External await timeout helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import TypeVar

from ..bridge.errors import BridgeExternalTimeout
from ..logging_utils import log_event

T = TypeVar("T")

DEFAULT_OPERATION_TIMEOUT_SECONDS = 30
MEDIA_TRANSFER_TIMEOUT_SECONDS = 120


async def with_timeout(
    awaitable: Awaitable[T],
    *,
    timeout_seconds: int | float,
    operation: str,
) -> T:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise BridgeExternalTimeout(
            operation=operation,
            timeout_seconds=timeout_seconds,
        ) from exc


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
        return await with_timeout(
            awaitable,
            timeout_seconds=timeout_seconds,
            operation=operation,
        )
    except BridgeExternalTimeout:
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
