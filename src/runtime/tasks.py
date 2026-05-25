"""Async task helpers for owned fire-and-forget work."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from collections.abc import Awaitable


def attach_task_logger(
    task: asyncio.Task,
    *,
    logger: logging.Logger,
    name: str | None = None,
) -> asyncio.Task:
    task_name = name or task.get_name()

    def _log_task_failure(done: asyncio.Task) -> None:
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        logger.error(
            "Detached task failed: %s",
            task_name,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    task.add_done_callback(_log_task_failure)
    return task


def create_logged_task(
    coro: Awaitable[object],
    *,
    logger: logging.Logger,
    name: str,
) -> asyncio.Task:
    return attach_task_logger(asyncio.create_task(coro, name=name), logger=logger, name=name)


async def cancel_and_wait(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
